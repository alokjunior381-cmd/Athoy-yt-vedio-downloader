from __future__ import annotations

import html
import json
import logging
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

LOG = logging.getLogger("streamly")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

TELEGRAM_TOKEN = os.getenv("TOKEN", "8833597828:AAE4P1eqD-eLOFlZOf3vYznBbMO8YgtHEWk").strip()
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

DOWNLOAD_OPTIONS: dict[int, dict[str, Any]] = {}
ACTIVE_CHATS: set[int] = set()
STATE_LOCK = threading.RLock()
EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="streamly")
SHUTDOWN = threading.Event()

def escape(value: Any) -> str:
    return html.escape(str(value), quote=False)

def http_request(url: str, method: str = "GET", payload: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    request = Request(url, data=payload, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=45) as response:
            return response.status, dict(response.headers.items()), response.read()
    except HTTPError as error:
        body = error.read().decode(errors="replace")
        raise RuntimeError(f"API Error ({error.code}): {body}") from error
    except Exception as error:
        raise RuntimeError(f"Network error: {error}") from error

def telegram_call(method: str, data: dict[str, Any] | None = None) -> Any:
    payload = (data or {}).copy()
    status, _, raw = http_request(
        f"{TELEGRAM_API}/{method}",
        method="POST",
        payload=urlencode(payload).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
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

def fetch_cobalt_media(source_url: str, is_audio: bool = False) -> str:
    # Cobalt পাবলিক API ব্যবহার করে ইউটিউব প্রসেস করা
    api_url = "https://api.cobalt.tools/api/json"
    payload = json.dumps({
        "url": source_url,
        "downloadMode": "audio" if is_audio else "auto",
        "videoQuality": "720"
    }).encode("utf-8")
    
    status, _, raw = http_request(
        api_url,
        method="POST",
        payload=payload,
        headers={"Content-Type": "application/json"}
    )
    data = json.loads(raw.decode("utf-8", errors="replace"))
    
    if data.get("status") == "error":
        raise RuntimeError(data.get("text", "ভিডিও প্রসেস করা সম্ভব হয়নি।"))
    
    if data.get("status") in ("tunnel", "redirect"):
        return data.get("url")
    
    raise RuntimeError("ডাউনলোড লিংক পাওয়া যায়নি।")

def resolve_download(chat_id: int, status_id: int, source_url: str) -> None:
    try:
        edit_message(chat_id, status_id, "<b>▶️ YouTube</b>\n\nCobalt API দিয়ে ভিডিও লিংক প্রসেস করা হচ্ছে...")
        video_url = fetch_cobalt_media(source_url, is_audio=False)
        
        with STATE_LOCK:
            DOWNLOAD_OPTIONS[chat_id] = {"video_url": video_url, "source_url": source_url}
        
        keyboard = json.dumps({
            "inline_keyboard": [
                [{"text": "🎬 Download Video", "callback_data": "dl:video"}],
                [{"text": "🎵 Download Audio", "callback_data": "dl:audio"}]
            ]
        })
        edit_message(chat_id, status_id, "<b>ভিডিও সফলভাবে প্রসেস হয়েছে!</b>\n\nফরম্যাট বাছুন:", reply_markup=keyboard)
    except Exception as error:
        edit_message(chat_id, status_id, f"<b>ব্যর্থ হয়েছে:</b>\n\n<code>{escape(str(error))}</code>")
    finally:
        with STATE_LOCK:
            ACTIVE_CHATS.discard(chat_id)

def send_download(chat_id: int, status_id: int, media_type: str) -> None:
    try:
        item = DOWNLOAD_OPTIONS.get(chat_id, {})
        source_url = item.get("source_url")
        
        if media_type == "audio":
            edit_message(chat_id, status_id, "<b>অডিও প্রসেস করা হচ্ছে...</b>")
            media_url = fetch_cobalt_media(source_url, is_audio=True)
            edit_message(chat_id, status_id, "<b>অডিও পাঠানো হচ্ছে...</b>")
            telegram_call("sendAudio", {"chat_id": str(chat_id), "audio": media_url})
        else:
            media_url = item.get("video_url")
            edit_message(chat_id, status_id, "<b>ভিডিও পাঠানো হচ্ছে...</b>")
            telegram_call("sendVideo", {"chat_id": str(chat_id), "video": media_url, "supports_streaming": "true"})
            
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
        status = send_message(chat_id, "প্রসেসিং শুরু হচ্ছে...")
        EXECUTOR.submit(resolve_download, chat_id, status["message_id"], text)

def process_callback(callback: dict[str, Any]) -> None:
    chat_id = callback.get("message", {}).get("chat", {}).get("id")
    message_id = callback.get("message", {}).get("message_id")
    data = callback.get("data", "")
    telegram_call("answerCallbackQuery", {"callback_query_id": callback.get("id")})

    if data.startswith("dl:"):
        media_type = data.split(":")[1]
        EXECUTOR.submit(send_download, chat_id, message_id, media_type)

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
