from __future__ import annotations

import base64
import html
import json
import logging
import os
import random
import re
import signal
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

import yt_dlp

LOG = logging.getLogger("streamly")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

TELEGRAM_TOKEN = os.getenv("TOKEN", "8833597828:AAE4P1eqD-eLOFlZOf3vYznBbMO8YgtHEWk").strip()
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MAX_MEDIA_BYTES = max(8, int(os.getenv("MAX_MEDIA_MB", "48"))) * 1024 * 1024
MAX_VIDEO_SECONDS = max(60, int(os.getenv("MAX_VIDEO_MINUTES", "30"))) * 60
STATE_TTL_SECONDS = 20 * 60
MAX_TEXT_LENGTH = 3900


@dataclass(frozen=True)
class Platform:
    key: str
    title: str
    emoji: str
    domains: tuple[str, ...]


YOUTUBE = Platform(
    "youtube",
    "YouTube",
    "▶",
    ("youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com"),
)
PLATFORM_BY_KEY = {YOUTUBE.key: YOUTUBE}

CHAT_MODES: dict[int, str] = {}
CHAT_PLATFORMS: dict[int, str] = {}
DOWNLOAD_OPTIONS: dict[int, dict[str, Any]] = {}
ACTIVE_CHATS: set[int] = set()
STATE_LOCK = threading.RLock()
EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="streamly")
SHUTDOWN = threading.Event()


def log(message: str, *args: Any) -> None:
    LOG.info(message, *args)


def format_bytes(value: int | float) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def escape(value: Any) -> str:
    return html.escape(str(value), quote=False)


def normalize_url(value: str) -> str | None:
    candidate = value.strip()
    if not re.match(r"^https?://", candidate, flags=re.IGNORECASE):
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if len(candidate) > 2048:
        return None
    return candidate


def platform_from_url(url: str) -> Platform | None:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    for platform in PLATFORM_BY_KEY.values():
        if any(host == domain or host.endswith(f".{domain}") for domain in platform.domains):
            return platform
    return None


def http_request(
    url: str,
    *,
    method: str = "GET",
    payload: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 35,
    max_bytes: int = 3 * 1024 * 1024,
) -> tuple[int, dict[str, str], bytes]:
    request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        request_headers.update(headers)
    request = Request(url, data=payload, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            chunks: list[bytes] = []
            total = 0
            while total < max_bytes:
                chunk = response.read(min(64 * 1024, max_bytes - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            return response.status, dict(response.headers.items()), b"".join(chunks)
    except HTTPError as error:
        body = error.read(1024)
        raise RuntimeError(
            f"Remote service returned HTTP {error.code}: "
            f"{body.decode(errors='replace')[:180]}"
        ) from error
    except (URLError, TimeoutError) as error:
        raise RuntimeError(f"Remote service could not be reached: {error}") from error


def telegram_call(method: str, data: dict[str, Any] | None = None) -> Any:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TOKEN is not configured")
    payload = (data or {}).copy()
    status, _, raw = http_request(
        f"{TELEGRAM_API}/{method}",
        method="POST",
        payload=urlencode(payload).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=45,
        max_bytes=4 * 1024 * 1024,
    )
    if status >= 400:
        raise RuntimeError(f"Telegram API HTTP {status}")
    try:
        result = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as error:
        raise RuntimeError("Telegram returned invalid JSON") from error
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram API request failed"))
    return result.get("result")


def send_message(chat_id: int | str, text: str, **extra: Any) -> Any:
    return telegram_call(
        "sendMessage",
        {"chat_id": str(chat_id), "text": text, "parse_mode": "HTML", **extra},
    )


def edit_message(chat_id: int | str, message_id: int, text: str, **extra: Any) -> Any:
    try:
        return telegram_call(
            "editMessageText",
            {
                "chat_id": str(chat_id),
                "message_id": str(message_id),
                "text": text,
                "parse_mode": "HTML",
                **extra,
            },
        )
    except RuntimeError as error:
        if "message is not modified" not in str(error).lower():
            log("Could not edit status message: %s", error)
        return None


def answer_callback(callback_id: str, text: str = "") -> None:
    try:
        telegram_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
    except RuntimeError as error:
        log("Callback acknowledgement failed: %s", error)


def inline_keyboard(rows: list[list[dict[str, str]]]) -> str:
    return json.dumps({"inline_keyboard": rows})


def main_keyboard() -> str:
    return inline_keyboard(
        [
            [{"text": "⬇️ Download YouTube", "callback_data": "mode:download"}],
            [{"text": "ℹ️ Help", "callback_data": "help"}, {"text": "✖️ Cancel", "callback_data": "cancel"}],
        ]
    )


def platform_keyboard() -> str:
    return inline_keyboard(
        [
            [{"text": "▶️ YouTube", "callback_data": "platform:youtube"}],
            [{"text": "↩️ Back to menu", "callback_data": "home"}],
        ]
    )


def quality_keyboard(options: list[dict[str, Any]]) -> str:
    rows: list[list[dict[str, str]]] = []
    for index, option in enumerate(options[:6]):
        size = option.get("size")
        suffix = f" · {format_bytes(size)}" if size else ""
        rows.append(
            [
                {
                    "text": f"🎚️ {option['label']}{suffix}",
                    "callback_data": f"quality:{index}",
                }
            ]
        )
    rows.append([{"text": "✖️ Cancel", "callback_data": "cancel"}])
    return inline_keyboard(rows)


def format_keyboard() -> str:
    return inline_keyboard(
        [
            [
                {"text": "🎬 MP4 video", "callback_data": "format:mp4"},
                {"text": "🎵 MP3 audio", "callback_data": "format:mp3"},
            ],
            [{"text": "↩️ Choose another quality", "callback_data": "back:quality"}],
        ]
    )


def progress_text(title: str, downloaded: int, total: int, stage: str) -> str:
    if total:
        percent = min(100, downloaded * 100 / total)
        filled = min(20, int(percent / 5))
        progress = f"{percent:5.1f}%"
        status = f"{format_bytes(downloaded)} of {format_bytes(total)}"
    else:
        filled = min(20, int(time.monotonic() * 3) % 21)
        progress = "working"
        status = f"{format_bytes(downloaded)} downloaded"
    bar = "█" * filled + "░" * (20 - filled)
    return (
        f"<b>📥 {escape(title[:70])}</b>\n\n"
        f"<code>{bar}</code>\n\n"
        f"🚀 <b>Progress:</b> {progress}\n"
        f"📶 <b>Status:</b> {escape(status)}\n"
        f"🛠 <b>Stage:</b> {escape(stage)}"
    )


def cleanup_state() -> None:
    cutoff = time.time() - STATE_TTL_SECONDS
    with STATE_LOCK:
        expired = [
            chat_id
            for chat_id, item in DOWNLOAD_OPTIONS.items()
            if float(item.get("created_at", 0)) < cutoff
        ]
        for chat_id in expired:
            DOWNLOAD_OPTIONS.pop(chat_id, None)
            CHAT_PLATFORMS.pop(chat_id, None)


def youtube_info(source_url: str) -> tuple[str, list[dict[str, Any]]]:
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'format': 'best',
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios'],
            }
        },
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(source_url, download=False)
        except Exception as error:
            raise RuntimeError(f"YouTube URL প্রসেস করা যায়নি: {error}")

    duration = info.get("duration", 0)
    if duration > MAX_VIDEO_SECONDS:
        raise RuntimeError(f"ভিডিওর দৈর্ঘ্য {duration // 60} মিনিট। সর্বোচ্চ সীমা {MAX_VIDEO_SECONDS // 60} মিনিট।")

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
                "format": fmt.get("format_id"),
                "url": fmt.get("url"),
                "audio_url": audio_url or fmt.get("url"),
                "height": height,
                "size": fmt.get("filesize") or fmt.get("filesize_approx") or 0,
                "ext": "mp4",
                "label": f"{height}p HD" if height >= 720 else f"{height}p SD",
            })
            if len(options) >= 4:
                break

    if not options and sorted_vids:
        fmt = sorted_vids[0]
        options.append({
            "format": fmt.get("format_id"),
            "url": fmt.get("url"),
            "audio_url": audio_url or fmt.get("url"),
            "height": fmt.get("height", 0),
            "size": fmt.get("filesize") or 0,
            "ext": "mp4",
            "label": "Auto Quality",
        })

    if not options:
        raise RuntimeError("ডাউনলোডের মতো কোনো ভিডিও ফরম্যাট পাওয়া যায়নি।")

    return title, options


def send_download(chat_id: int, status_id: int, source_url: str, option: dict[str, Any]) -> None:
    try:
        output_format = str(option["output_format"])
        media_url = option.get("audio_url" if output_format == "mp3" else "url")
        title = str(option.get("title") or "YouTube Media")
        
        edit_message(chat_id, status_id, progress_text(title, 100, 100, "Sending to Telegram..."))
        
        caption = f"<b>{escape(title[:900])}</b>"
        if output_format == "mp3":
            telegram_call("sendAudio", {"chat_id": str(chat_id), "audio": media_url, "caption": caption, "title": title[:200]})
        else:
            telegram_call("sendVideo", {"chat_id": str(chat_id), "video": media_url, "caption": caption, "supports_streaming": "true"})
            
        edit_message(chat_id, status_id, "<b>Download Complete!</b>", reply_markup=main_keyboard())
    except Exception as error:
        log("Download failed: %s", error)
        edit_message(chat_id, status_id, f"<b>ব্যর্থ হয়েছে:</b>\n\n{escape(str(error))}", reply_markup=platform_keyboard())
    finally:
        with STATE_LOCK:
            ACTIVE_CHATS.discard(chat_id)


def resolve_download(chat_id: int, status_id: int, source_url: str) -> None:
    try:
        edit_message(chat_id, status_id, "<b>▶️ YouTube</b>\n\n১/৩  যাচাই করা হচ্ছে...")
        title, options = youtube_info(source_url)
        with STATE_LOCK:
            DOWNLOAD_OPTIONS[chat_id] = {"title": title, "options": options, "source_url": source_url, "created_at": time.time()}
        edit_message(
            chat_id, status_id,
            f"<b>▶️ {escape(title[:80])}</b>\n\nএকটিকে বেছে নিন:",
            reply_markup=quality_keyboard(options)
        )
    except Exception as error:
        edit_message(chat_id, status_id, f"<b>ত্রুটি:</b>\n\n{escape(str(error))}", reply_markup=platform_keyboard())


def process_message(message: dict[str, Any]) -> None:
    chat_id = (message.get("chat") or {}).get("id")
    if not chat_id:
        return
    text = (message.get("text") or "").strip()
    
    if text == "/start":
        send_message(chat_id, "<b>স্বাগতম!</b>\nভিডিও ডাউনলোড করতে নিচের অপশনে চাপ দিন।", reply_markup=main_keyboard())
        return

    url = normalize_url(text)
    if url:
        with STATE_LOCK:
            ACTIVE_CHATS.add(chat_id)
        status = send_message(chat_id, "<b>▶️ YouTube</b>\n\nপ্রসেসিং শুরু হচ্ছে...")
        EXECUTOR.submit(resolve_download, chat_id, status["message_id"], url)
    else:
        send_message(chat_id, "সঠিক একটি YouTube URL দিন।", reply_markup=main_keyboard())


def process_callback(callback: dict[str, Any]) -> None:
    callback_id = callback.get("id", "")
    data = callback.get("data", "")
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    answer_callback(callback_id)
    if not chat_id or not message_id:
        return

    if data == "home":
        edit_message(chat_id, message_id, "<b>প্রধান মেনু</b>", reply_markup=main_keyboard())
    elif data == "cancel":
        edit_message(chat_id, message_id, "বাতিল করা হয়েছে।", reply_markup=main_keyboard())
    elif data == "mode:download" or data == "platform:youtube":
        CHAT_MODES[chat_id] = "awaiting_url"
        edit_message(chat_id, message_id, "<b>▶️ YouTube selected</b>\n\nএখন ইউটিউব ভিডিওর লিংকটি পাঠান।")
    elif data.startswith("quality:"):
        item = DOWNLOAD_OPTIONS.get(chat_id)
        index = int(data.split(":", 1)[1])
        option = item["options"][index]
        option["title"] = item["title"]
        DOWNLOAD_OPTIONS[chat_id]["selected"] = option
        edit_message(chat_id, message_id, f"<b>ফরম্যাট বাছুন:</b>\n\n{escape(option['label'])}", reply_markup=format_keyboard())
    elif data.startswith("format:"):
        output_format = data.split(":", 1)[1]
        item = DOWNLOAD_OPTIONS.get(chat_id)
        selected = item.get("selected")
        selected["output_format"] = output_format
        EXECUTOR.submit(send_download, chat_id, message_id, item["source_url"], selected)


def polling_loop() -> None:
    offset = 0
    while not SHUTDOWN.is_set():
        try:
            updates = telegram_call("getUpdates", {"offset": str(offset), "timeout": "25"})
            for update in updates or []:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                if update.get("callback_query"):
                    process_callback(update["callback_query"])
                elif update.get("message"):
                    process_message(update["message"])
        except Exception as error:
            time.sleep(3)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    telegram_call("deleteWebhook", {"drop_pending_updates": "false"})
    polling_loop()

if __name__ == "__main__":
    main()
