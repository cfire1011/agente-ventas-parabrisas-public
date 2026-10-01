from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from . import legacy

send_message = legacy.send_msg
send_typing = legacy.typing
user_name = legacy.user_name
messenger_status = legacy.messenger_status
parse_signed_request = legacy.parse_signed_request


def verify_signature(body: bytes, signature: str | None, app_secret: str | None) -> bool:
    if not app_secret:
        return True
    expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return bool(signature) and hmac.compare_digest(signature, expected)


def iter_messenger_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for entry in payload.get("entry", []) or []:
        for event in entry.get("messaging", []) or []:
            events.append(event)
    return events


def parse_event_message(event: dict[str, Any]) -> tuple[str, str, list[str]]:
    sender = (event.get("sender") or {}).get("id") or ""
    message = event.get("message") or {}
    text = message.get("text") or ""
    if event.get("postback"):
        text = event["postback"].get("title") or event["postback"].get("payload") or text
    images = [
        a["payload"]["url"]
        for a in message.get("attachments", [])
        if a.get("type") == "image" and a.get("payload", {}).get("url")
    ]
    return str(sender), str(text or ""), images


def dumps_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)
