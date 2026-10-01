from __future__ import annotations

import logging

log = logging.getLogger("llm_status")

_disabled_reason = ""


def is_disabled() -> bool:
    return bool(_disabled_reason)


def disabled_reason() -> str:
    return _disabled_reason


def disable(reason: str) -> None:
    global _disabled_reason
    if _disabled_reason:
        return
    _disabled_reason = reason
    log.warning("LLM disabled: %s", reason)
