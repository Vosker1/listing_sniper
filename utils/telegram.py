"""
Telegram Notifier - Non-blocking notifications
"""

import os
import time
import threading
import queue
import json
import html
from typing import Optional, Dict, Tuple
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv(*args, **kwargs):
        return False

import requests

# Global state
_TELEGRAM_ENABLED: bool = False
_BOT_TOKEN: Optional[str] = None
_CHAT_ID: Optional[str] = None

# Rate limit
_RATE_MAX_PER_MIN = 20
_rate_tokens = _RATE_MAX_PER_MIN
_rate_last_refill = time.time()

# Async sender
_q: "queue.Queue[Tuple[str, Dict[str,str]]]" = queue.Queue(maxsize=256)
_sender_thread: Optional[threading.Thread] = None
_session: Optional[requests.Session] = None


def _refill_tokens() -> None:
    global _rate_tokens, _rate_last_refill
    now = time.time()
    if now - _rate_last_refill >= 60:
        _rate_tokens = _RATE_MAX_PER_MIN
        _rate_last_refill = now


def _acquire_token() -> bool:
    global _rate_tokens
    _refill_tokens()
    if _rate_tokens > 0:
        _rate_tokens -= 1
        return True
    return False


def _log(msg: str) -> None:
    print(f"[TG] {msg}")


def _start_sender_if_needed() -> None:
    global _sender_thread, _session
    if _sender_thread and _sender_thread.is_alive():
        return
    _session = requests.Session()
    _session.headers.update({"Content-Type": "application/json"})
    _sender_thread = threading.Thread(target=_sender_loop, name="telegram-sender", daemon=True)
    _sender_thread.start()
    _log("sender thread started")


def _sender_loop() -> None:
    assert _session is not None
    while True:
        try:
            method, payload = _q.get()
            if method == "__STOP__":
                break
            _send_api_sync(method, payload)
        except Exception as e:
            _log(f"send error: {e!r}")


def _enqueue(method: str, payload: Dict[str, str]) -> None:
    try:
        _q.put_nowait((method, payload))
    except queue.Full:
        _log("queue full, dropping message")


def _send_api_sync(method: str, payload: Dict[str, str]) -> None:
    global _session
    if not _session:
        _session = requests.Session()
    url = f"https://api.telegram.org/bot{_BOT_TOKEN}/{method}"
    for attempt in range(3):
        try:
            r = _session.post(url, data=json.dumps(payload), timeout=7)
            if r.status_code == 200:
                return
        except Exception as e:
            _log(f"attempt {attempt+1} error: {e!r}")
        time.sleep(0.8 + attempt * 0.7)


def _to_html_pre(text: str) -> str:
    return f"<pre>{html.escape(text)}</pre>"


def setup_from_env(env_path: str = "input.env") -> None:
    """Load telegram config from env"""
    p = Path(env_path)
    if p.exists():
        load_dotenv(env_path)

    global _TELEGRAM_ENABLED, _BOT_TOKEN, _CHAT_ID
    _TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() in ("1", "true", "yes")
    _BOT_TOKEN = os.getenv("BOT_TOKEN")
    _CHAT_ID = os.getenv("CHAT_ID")

    if not _TELEGRAM_ENABLED:
        _log("disabled via TELEGRAM_ENABLED")
        return
    if not _BOT_TOKEN or not _CHAT_ID:
        _log("missing BOT_TOKEN or CHAT_ID; disabling")
        _TELEGRAM_ENABLED = False
        return

    _start_sender_if_needed()
    _log("ready")


def enabled() -> bool:
    return bool(_TELEGRAM_ENABLED and _BOT_TOKEN and _CHAT_ID)


def send_message(text: str, disable_notification: bool = False) -> None:
    """Send a text message (non-blocking)"""
    if not enabled():
        return
    if not _acquire_token():
        _log("rate-limited, drop")
        return
    payload = {
        "chat_id": _CHAT_ID or "",
        "parse_mode": "HTML",
        "disable_notification": disable_notification,
        "text": _to_html_pre(text),
    }
    _enqueue("sendMessage", payload)


def send_alert(text: str) -> None:
    """Send alert (with notification)"""
    send_message(text, disable_notification=False)


def shutdown() -> None:
    """Stop sender thread"""
    try:
        _enqueue("__STOP__", {})
    except Exception:
        pass
