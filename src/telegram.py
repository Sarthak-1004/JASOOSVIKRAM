"""Minimal Telegram Bot API client."""
import json
from pathlib import Path

import requests

from .common import env, log

API = "https://api.telegram.org/bot{token}/{method}"


def _call(method: str, files=None, http_timeout: int = 60, **params):
    url = API.format(token=env("TELEGRAM_BOT_TOKEN"), method=method)
    for k, v in list(params.items()):
        if isinstance(v, (dict, list)):
            params[k] = json.dumps(v, ensure_ascii=False)
    try:
        r = requests.post(url, data=params, files=files, timeout=http_timeout)
        data = r.json()
        if not data.get("ok"):
            log.warning("Telegram %s failed: %s", method, data)
        return data.get("result")
    except Exception as e:  # noqa: BLE001
        log.warning("Telegram %s error: %s", method, e)
        return None


def chat_id() -> str:
    return env("TELEGRAM_CHAT_ID")


def send(text: str, buttons: list[list[tuple[str, str]]] | None = None) -> None:
    params = {"chat_id": chat_id(), "text": text[:4000], "disable_web_page_preview": True}
    if buttons:
        params["reply_markup"] = {"inline_keyboard": [
            [{"text": t, "callback_data": d} for t, d in row] for row in buttons]}
    _call("sendMessage", **params)


def send_photo(path: Path, caption: str = "") -> None:
    with open(path, "rb") as f:
        _call("sendPhoto", files={"photo": f}, http_timeout=300, chat_id=chat_id(), caption=caption[:1000])


def send_video(path: Path, caption: str = "") -> None:
    with open(path, "rb") as f:
        _call("sendVideo", files={"video": f}, http_timeout=600, chat_id=chat_id(),
              caption=caption[:1000], supports_streaming=True)


def send_document(path: Path, caption: str = "", filename: str | None = None) -> None:
    with open(path, "rb") as f:
        _call("sendDocument", files={"document": (filename or path.name, f)}, http_timeout=900,
              chat_id=chat_id(), caption=caption[:1000])


def send_audio(path: Path, caption: str = "") -> None:
    with open(path, "rb") as f:
        _call("sendAudio", files={"audio": f}, http_timeout=300, chat_id=chat_id(), caption=caption[:1000])


def updates(offset: int | None, timeout: int = 50) -> list[dict]:
    params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        params["offset"] = offset
    res = _call("getUpdates", http_timeout=timeout + 15, **params)
    return res or []


def answer_callback(cb_id: str, text: str = "") -> None:
    _call("answerCallbackQuery", callback_query_id=cb_id, text=text)
