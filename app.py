from __future__ import annotations

import html
import json
import logging
import os
import re
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

import yt_dlp

LOG = logging.getLogger("streamly")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

TELEGRAM_TOKEN = os.getenv("TOKEN", "8833597828:AAE4P1eqD-eLOFlZOf3vYznBbMO8YgtHEWk").strip()
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

MAX_MEDIA_BYTES = 48 * 1024 * 1024
MAX_VIDEO_SECONDS = 30 * 60
STATE_TTL_SECONDS = 20 * 60

CHAT_MODES: dict[int, str] = {}
DOWNLOAD_OPTIONS: dict[int, dict[str, Any]] = {}
ACTIVE_CHATS: set[int] = set()
STATE_LOCK = threading.RLock()
EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="streamly")
SHUTDOWN = threading.Event()

def escape(value: Any) -> str:
    return html.escape(str(value), quote=False)

def format_bytes(value: int | float) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"

def http_request(url: str, method: str = "GET", payload: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    request = Request(url, data=payload, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=45) as response:
            return response.status, dict(response.headers.items()), response.read()
    except Exception as error:
        raise RuntimeError(f"Network error: {error}") from error

def telegram_call(method: str, data: dict[str, Any] | None = None) -> Any:
    payload = (data or {}).copy()
    status, _, raw = http_request(f"{TELEGRAM_API}/{method}", method="POST", payload=urlencode(payload).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})
    result = json.loads(raw.decode("utf-8", errors="replace"))
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram API Error"))
    return result.get("result")

def send_message(chat_id: int | str, text: str, reply_markup: str | None = None) -> Any:
    data = {"chat_id": str(chat_id), "text": text, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = reply_markup
    return telegram_call("sendMessage", data)

def edit_message(chat_id: int | str, message_id: int, text: str, reply_markup: str | None = None) -> Any:
    data = {"chat_id": str(chat_id), "message_id": str(message_id), "text": text, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = reply_markup
    try:
        return telegram_call("editMessageText", data)
    except Exception:
        pass

def youtube_info(source_url: str) -> tuple[str, list[dict[str, Any]]]:
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'format': 'best',
        'cookiefile': 'cookies.txt',  # কুকিজ ফাইল ব্যবহার করা হচ্ছে
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(source_url, download=False)
        except Exception as error:
            raise RuntimeError(f"YouTube Error: {error}")

    title = info.get("title", "YouTube Video")
    formats = info.get("formats", [])
    
    options: list[dict[str, Any]] = []
    video_formats = [f for f in formats if f.get("vcodec") != "none" and f.get("url")]
    audio_formats = [f for f in formats if f.get("acodec") != "none" and f.get("vcodec") == "none" and f.get("url")]
    
    audio_url = audio_formats[-1].get("url") if audio_formats else ""
    sorted_vids = sorted(video_formats, key=lambda x: x.get("height") or 0, reverse=True)
    seen_heights = set()

    for fmt in sorted_vids:
        height = fmt.get("height", 0)
        if height and height not in seen_heights and height <= 1080:
            seen_heights.add(height)
            options.append({
                "url": fmt.get("url"),
                "audio_url": audio_url or fmt.get("url"),
                "height": height,
                "size": fmt.get("filesize") or fmt.get("filesize_approx") or 0,
                "label": f"{height}p",
            })
            if len(options) >= 3:
                break

    if not options:
        raise RuntimeError("কোনো ডাউনলোড লিংক পাওয়া যায়নি।")

    return title, options

def resolve_download(chat_id: int, status_id: int, source_url: str) -> None:
    try:
        edit_message(chat_id, status_id, "<b>▶️ YouTube</b>\n\nকুকিজ দিয়ে লিংক প্রসেস হচ্ছে...")
        title, options = youtube_info(source_url)
        with STATE_LOCK:
            DOWNLOAD_OPTIONS[chat_id] = {"title": title, "options": options, "source_url": source_url, "created_at": time.time()}
        
        keyboard = json.dumps({"inline_keyboard": [[{"text": f"🎬 {o['label']} ({format_bytes(o['size'])})", "callback_data": f"dl:{i}"}] for i, o in enumerate(options)]})
        edit_message(chat_id, status_id, f"<b>{escape(title[:80])}</b>\n\nQuality বেছে নিন:", reply_markup=keyboard)
    except Exception as error:
        edit_message(chat_id, status_id, f"<b>ফেইল হয়েছে:</b>\n\n<code>{escape(str(error))}</code>")
    finally:
        with STATE_LOCK:
            ACTIVE_CHATS.discard(chat_id)

def send_download(chat_id: int, status_id: int, option: dict[str, Any]) -> None:
    try:
        edit_message(chat_id, status_id, "<b>পাঠানো হচ্ছে...</b>")
        telegram_call("sendVideo", {"chat_id": str(chat_id), "video": option["url"], "caption": f"<b>{escape(option['title'])}</b>", "supports_streaming": "true"})
        edit_message(chat_id, status_id, "<b>ডাউনলোড সম্পন্ন!</b>")
    except Exception as error:
        edit_message(chat_id, status_id, f"<b>পাঠাতে ব্যর্থ:</b> {escape(str(error))}")

def process_message(message: dict[str, Any]) -> None:
    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()
    if not chat_id or not text:
        return

    if text == "/start":
        send_message(chat_id, "<b>বট রেডি!</b>\nইউটিউব ভিডিওর লিংক পাঠান।")
        return

    if "youtube.com" in text or "youtu.be" in text:
        with STATE_LOCK:
            ACTIVE_CHATS.add(chat_id)
        status = send_message(chat_id, "প্রসেসিং...")
        EXECUTOR.submit(resolve_download, chat_id, status["message_id"], text)

def process_callback(callback: dict[str, Any]) -> None:
    chat_id = callback.get("message", {}).get("chat", {}).get("id")
    message_id = callback.get("message", {}).get("message_id")
    data = callback.get("data", "")
    telegram_call("answerCallbackQuery", {"callback_query_id": callback.get("id")})

    if data.startswith("dl:"):
        index = int(data.split(":")[1])
        item = DOWNLOAD_OPTIONS.get(chat_id, {})
        option = item.get("options", [])[index]
        option["title"] = item.get("title")
        EXECUTOR.submit(send_download, chat_id, message_id, option)

def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), BaseHTTPRequestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    telegram_call("deleteWebhook", {"drop_pending_updates": "false"})
    
    offset = 0
    while not SHUTDOWN.is_set():
        try:
            updates = telegram_call("getUpdates", {"offset": str(offset), "timeout": "20"})
            for update in updates or []:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                if "callback_query" in update:
                    process_callback(update["callback_query"])
                elif "message" in update:
                    process_message(update["message"])
        except Exception:
            time.sleep(2)

if __name__ == "__main__":
    main()
