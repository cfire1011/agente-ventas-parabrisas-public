from __future__ import annotations

import json

from . import legacy


def draft_reply(lead: dict, user_text: str, action: dict) -> str:
    """El LLM solo redacta; flow.py decide QUÉ decir."""
    lang = legacy._client_lang(lead, user_text, psid=lead.get("psid"))
    target = {
        "goal": action.get("goal"),
        "reply": action.get("reply"),
        "cta": action.get("cta"),
        "language": "english" if lang == "en" else "spanish",
    }
    messages = [
        {"role": "system", "content": legacy._system_prompt()},
        {
            "role": "system",
            "content": (
                "Redacta SOLO el texto final al cliente. No cambies la decisión de negocio. "
                "No pidas nombre, calle, teléfono ni tarjeta. Si piden contacto: puedes llamarnos por Facebook. Mantén 1-2 frases. "
                + json.dumps(target, ensure_ascii=False)
            ),
        },
        {"role": "user", "content": user_text or "hola"},
    ]
    try:
        raw, _finish = legacy._llm(messages, max_tokens=180)
        data = legacy._parse(raw)
        reply = (data.get("reply") or action.get("reply") or "").strip()
        return legacy._ensure_complete_reply(reply, lead, truncated_flag=False)
    except Exception:
        return legacy._ensure_complete_reply(action.get("reply") or "", lead, truncated_flag=False)
