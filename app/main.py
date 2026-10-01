from __future__ import annotations

import logging
import os
import sys

import uvicorn
from fastapi import FastAPI

from .config import NEGOCIO
from .dashboard_api import router as dashboard_router
from .db import init_db
from .flow import run_agent
from .webhook_api import router as webhook_router

app = FastAPI(title=NEGOCIO)

app.include_router(dashboard_router)
app.include_router(webhook_router)


@app.on_event("startup")
def startup() -> None:
    init_db()
    try:
        from .memory import bootstrap
        bootstrap()
        logging.getLogger("agente").info("memory bootstrap done")
    except Exception:
        logging.getLogger("agente").exception("memory bootstrap")
    try:
        from .human_takeover import strip_meta_greetings
        got = strip_meta_greetings()
        logging.getLogger("agente").info("meta greetings off: %s", got)
    except Exception:
        logging.getLogger("agente").exception("strip_meta_greetings")
    try:
        from .nudge import start_nudge_loop
        start_nudge_loop()
    except Exception:
        logging.getLogger("agente").exception("nudge loop")
    try:
        from .fb_inbox import start_sync_loop
        start_sync_loop()
    except Exception:
        logging.getLogger("agente").exception("fb inbox sync")


def chat_cli() -> None:
    init_db()
    psid = "local_test"
    print("Chat local (salir = exit). Citas se guardan en citas.txt\n")
    while True:
        try:
            msg = input("Cliente: ").strip()
        except EOFError:
            break
        if msg.lower() in ("exit", "salir", "quit"):
            break
        print("Bot:", run_agent(psid, msg), "\n")


def main(argv: list[str] | None = None) -> None:
    argv = argv or sys.argv[1:]
    if argv and argv[0] == "chat":
        chat_cli()
        return
    uvicorn.run("bot:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
