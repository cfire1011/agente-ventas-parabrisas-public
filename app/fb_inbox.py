"""Inbox real de Facebook Messenger: webhook crudo + Graph API."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import httpx

from . import legacy

log = logging.getLogger("agente.fb_inbox")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

GRAPH = legacy.GRAPH
SYNC_INTERVAL_SEC = int(os.getenv("FB_SYNC_INTERVAL_SEC", "45") or "45")
CONV_LIMIT = int(os.getenv("FB_SYNC_CONV_LIMIT", "400") or "400")
MSG_LIMIT = int(os.getenv("FB_SYNC_MSG_LIMIT", "80") or "80")
# Ventana de catch-up: si el bot/servidor estuvo caido (Mac reiniciada, sin
# internet, etc) mas tiempo que este limite, los mensajes que llegaron
# durante la caida se dejaban de contestar PARA SIEMPRE (el chequeo de edad
# nunca vuelve a ser True). Se sube de 23h a 5 dias para que una caida de
# fin de semana no pierda leads reales.
CATCHUP_MIN_AGE_SEC = float(os.getenv("FB_CATCHUP_MIN_AGE_SEC", "20") or "20")
CATCHUP_MAX_AGE_SEC = float(os.getenv("FB_CATCHUP_MAX_AGE_SEC", str(5 * 24 * 3600)) or str(5 * 24 * 3600))
# Cuantos leads estancados se procesan por corrida de sync (cada ~45s). Se
# sube de 8 a 25 para vaciar mas rapido el backlog tras una caida larga.
CATCHUP_LIMIT = int(os.getenv("FB_CATCHUP_LIMIT", "25") or "25")

_MSG_FIELDS = (
    "id,created_time,from,to,message,attachments,sticker,tags,shares"
)
_CONV_FIELDS = (
    f"id,updated_time,message_count,unread_count,link,participants,"
    f"messages.limit({MSG_LIMIT}){{{_MSG_FIELDS}}}"
)

_loop_started = False
_backfill_done = False


def init_tables() -> None:
    with legacy.db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS fb_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at TEXT,
                page_id TEXT,
                event_type TEXT,
                sender_id TEXT,
                recipient_id TEXT,
                mid TEXT,
                timestamp_ms TEXT,
                raw TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS fb_messages (
                mid TEXT PRIMARY KEY,
                conversation_id TEXT,
                psid TEXT,
                sender_id TEXT,
                sender_name TEXT,
                direction TEXT,
                text TEXT,
                created_at TEXT,
                attachments TEXT,
                source TEXT,
                raw TEXT
            )"""
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_fb_msg_psid ON fb_messages(psid)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_fb_msg_created ON fb_messages(created_at)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_fb_evt_mid ON fb_events(mid)"
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = str(value).replace("+0000", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _page_id() -> str:
    return str(os.getenv("PAGE_ID", ""))


def _event_type(event: dict[str, Any]) -> str:
    if event.get("delivery"):
        return "delivery"
    if event.get("read"):
        return "read"
    if event.get("reaction"):
        return "reaction"
    if event.get("postback"):
        return "postback"
    msg = event.get("message") or {}
    if msg.get("is_echo"):
        return "echo"
    if msg:
        return "message"
    return "other"


def _mid_from_event(event: dict[str, Any]) -> str:
    msg = event.get("message") or {}
    mid = msg.get("mid") or event.get("mid") or ""
    if mid:
        return str(mid)
    raw = json.dumps(event, sort_keys=True, ensure_ascii=False)
    return "evt_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _attachments_json(value: Any) -> str:
    if not value:
        return "[]"
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return "[]"


def upsert_message(
    *,
    mid: str,
    conversation_id: str = "",
    psid: str = "",
    sender_id: str = "",
    sender_name: str = "",
    direction: str = "",
    text: str = "",
    created_at: str = "",
    attachments: Any = None,
    source: str = "",
    raw: Any = None,
) -> None:
    if not mid:
        return
    raw_s = raw if isinstance(raw, str) else json.dumps(raw or {}, ensure_ascii=False)
    with legacy.db() as c:
        existing = c.execute(
            "SELECT mid, text, attachments, raw, conversation_id, psid, "
            "sender_id, sender_name, direction, created_at, source "
            "FROM fb_messages WHERE mid=?",
            (mid,),
        ).fetchone()
        if existing:
            # Insert-only: reescribir 5k filas cada 45s corrompe SQLite
            # en el bind-mount de Docker Desktop. Solo rellena psid vacío.
            if source == "local":
                return
            if psid and not (existing["psid"] or ""):
                c.execute(
                    "UPDATE fb_messages SET psid=? WHERE mid=? AND (psid IS NULL OR psid='')",
                    (psid, mid),
                )
            return
        c.execute(
            """INSERT INTO fb_messages(
                mid, conversation_id, psid, sender_id, sender_name, direction,
                text, created_at, attachments, source, raw
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                mid,
                conversation_id,
                psid,
                sender_id,
                sender_name,
                direction,
                text or "",
                created_at or _now(),
                _attachments_json(attachments),
                source,
                raw_s,
            ),
        )


def ingest_event(event: dict[str, Any], *, page_id: str = "", source: str = "webhook") -> None:
    page_id = page_id or _page_id()
    sender = str(((event.get("sender") or {}).get("id")) or "")
    recipient = str(((event.get("recipient") or {}).get("id")) or "")
    msg = event.get("message") or {}
    postback = event.get("postback") or {}
    etype = _event_type(event)
    mid = _mid_from_event(event)
    ts = event.get("timestamp")
    created = ""
    if ts:
        try:
            created = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).isoformat()
        except Exception:
            created = str(ts)
    text = msg.get("text") or postback.get("title") or postback.get("payload") or ""
    if etype == "delivery":
        text = json.dumps(event.get("delivery") or {}, ensure_ascii=False)
    elif etype == "read":
        text = json.dumps(event.get("read") or {}, ensure_ascii=False)
    psid = recipient if sender == page_id else sender
    direction = "out" if sender == page_id or msg.get("is_echo") else "in"
    if etype in ("delivery", "read"):
        direction = "meta"
        psid = recipient if recipient != page_id else sender

    with legacy.db() as c:
        c.execute(
            """INSERT INTO fb_events(
                received_at, page_id, event_type, sender_id, recipient_id,
                mid, timestamp_ms, raw
            ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                _now(),
                page_id,
                etype,
                sender,
                recipient,
                mid,
                str(ts or ""),
                json.dumps(event, ensure_ascii=False),
            ),
        )

    if etype in ("message", "echo", "postback"):
        upsert_message(
            mid=mid,
            psid=psid,
            sender_id=sender,
            sender_name="",
            direction=direction,
            text=str(text or ""),
            created_at=created,
            attachments=msg.get("attachments") or [],
            source=source,
            raw=event,
        )


def ingest_payload(payload: dict[str, Any], *, source: str = "webhook") -> int:
    n = 0
    for entry in payload.get("entry") or []:
        page_id = str(entry.get("id") or _page_id())
        for event in entry.get("messaging") or []:
            try:
                ingest_event(event, page_id=page_id, source=source)
                n += 1
            except Exception:
                log.exception("ingest event")
    return n


def _graph_get(path: str, **params) -> dict[str, Any]:
    q = legacy._graph_params(**params)
    tok = q.pop("access_token", "")
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    r = httpx.get(
        f"{GRAPH}/{path.lstrip('/')}",
        params=q,
        headers=headers,
        timeout=25,
        trust_env=False,
    )
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        err = (data.get("error") or {}) if isinstance(data, dict) else {}
        raise RuntimeError(err.get("message") or r.text[:240])
    return data if isinstance(data, dict) else {}


def _save_graph_message(conv_id: str, page_id: str, item: dict[str, Any]) -> None:
    mid = str(item.get("id") or "")
    if not mid:
        return
    frm = item.get("from") or {}
    sender_id = str(frm.get("id") or "")
    sender_name = str(frm.get("name") or "")
    participants = []
    direction = "out" if sender_id == page_id else "in"
    to_data = ((item.get("to") or {}).get("data")) or []
    if direction == "in":
        psid = sender_id
    else:
        psid = ""
        for p in to_data:
            pid = str(p.get("id") or "")
            if pid and pid != page_id:
                psid = pid
                break
    atts = []
    for a in ((item.get("attachments") or {}).get("data")) or []:
        atts.append(a)
    upsert_message(
        mid=mid,
        conversation_id=conv_id,
        psid=psid,
        sender_id=sender_id,
        sender_name=sender_name,
        direction=direction,
        text=str(item.get("message") or ""),
        created_at=str(item.get("created_time") or ""),
        attachments=atts,
        source="graph",
        raw=item,
    )
    _ = participants


def _sync_one_user(psid: str, page_id: str) -> tuple[int, int]:
    payload = _graph_get(
        "me/conversations",
        platform="messenger",
        user_id=psid,
        fields=_CONV_FIELDS,
    )
    convs = 0
    msgs = 0
    for conv in payload.get("data") or []:
        convs += 1
        cid = str(conv.get("id") or "")
        for item in ((conv.get("messages") or {}).get("data")) or []:
            if str((item.get("from") or {}).get("id") or "") == page_id:
                item = dict(item)
                item.setdefault("to", {"data": [{"id": psid}]})
            _save_graph_message(cid, page_id, item)
            msgs += 1
    return convs, msgs


def backfill_local_history() -> int:
    """Copia history local (sin timestamp de Facebook) si Graph aún no trajo el mid."""
    init_tables()
    n = 0
    with legacy.db() as c:
        existing = {
            r["mid"]
            for r in c.execute("SELECT mid FROM fb_messages WHERE source='local'")
        }
    for lead in legacy.list_leads():
        psid = str(lead.get("psid") or "")
        try:
            hist = json.loads(lead.get("history") or "[]")
        except Exception:
            hist = []
        for i, item in enumerate(hist):
            if not isinstance(item, dict):
                continue
            text = str(item.get("content") or "")
            role = str(item.get("role") or "")
            if not text:
                continue
            mid = "local_" + hashlib.sha1(f"{psid}:{i}:{role}:{text}".encode()).hexdigest()[:20]
            if mid in existing:
                continue
            try:
                upsert_message(
                    mid=mid,
                    psid=psid,
                    sender_id=psid if role == "user" else _page_id(),
                    sender_name=lead.get("name") or "",
                    direction="in" if role == "user" else "out",
                    text=text,
                    created_at=str(lead.get("updated_at") or _now()),
                    attachments=[],
                    source="local",
                    raw={"role": role, "content": text, "psid": psid},
                )
                existing.add(mid)
                n += 1
            except Exception:
                log.exception("backfill mid=%s psid=%s", mid, psid[-4:] if psid else "")
    return n


def sync_from_graph(max_conversations: int | None = None) -> dict[str, Any]:
    init_tables()
    page_id = _page_id()
    page_name = None
    errors: list[str] = []
    try:
        page = _graph_get(page_id, fields="id,name")
        page_id = str(page.get("id") or page_id)
        page_name = page.get("name")
    except Exception as e:
        errors.append(f"page: {str(e)[:160]}")

    convs = 0
    msgs = 0
    cap = max_conversations or CONV_LIMIT

    # 1) Intento listado global de la Page.
    after = None
    listed = False
    while convs < cap:
        params = {
            "platform": "messenger",
            "fields": _CONV_FIELDS,
            "limit": min(25, cap - convs),
        }
        if after:
            params["after"] = after
        try:
            payload = _graph_get(f"{page_id}/conversations", **params)
        except Exception as e:
            errors.append(f"list: {str(e)[:160]}")
            break
        listed = True
        batch = payload.get("data") or []
        if not batch:
            break
        for conv in batch:
            convs += 1
            cid = str(conv.get("id") or "")
            people = ((conv.get("participants") or {}).get("data")) or []
            user_psid = ""
            for p in people:
                pid = str(p.get("id") or "")
                if pid and pid != page_id:
                    user_psid = pid
                    break
            for item in ((conv.get("messages") or {}).get("data")) or []:
                if user_psid and str((item.get("from") or {}).get("id") or "") == page_id:
                    item = dict(item)
                    item.setdefault("to", {"data": [{"id": user_psid}]})
                try:
                    _save_graph_message(cid, page_id, item)
                    msgs += 1
                except Exception as e:
                    errors.append(str(e)[:160])
            if convs >= cap:
                break
        after = ((payload.get("paging") or {}).get("cursors") or {}).get("after")
        if not after:
            break

    # 2) Fallback: un chat por lead (funciona con pages_messaging).
    if not listed or msgs == 0:
        psids = [str(l.get("psid") or "") for l in legacy.list_leads()]
        psids = [p for p in psids if p.isdigit() and len(p) >= 5]
        for psid in psids[:cap]:
            try:
                c_n, m_n = _sync_one_user(psid, page_id)
                convs += c_n
                msgs += m_n
            except Exception as e:
                errors.append(f"{psid[-4:]}: {str(e)[:120]}")
                if len(errors) > 12:
                    break

    global _backfill_done
    local_n = 0
    if not _backfill_done:
        local_n = backfill_local_history()
        _backfill_done = True
    catchup_n = catch_up_missed_inbound()
    retry_n = retry_failed_sends(limit=3)
    legacy.set_setting("fb_sync_at", _now())
    return {
        "ok": msgs > 0 or local_n > 0,
        "page_id": page_id,
        "page_name": page_name,
        "conversations": convs,
        "messages": msgs,
        "local_backfill": local_n,
        "catchup_sent": catchup_n,
        "retry_sent": retry_n,
        "errors": errors[:8],
        "synced_at": _now(),
    }


def catch_up_missed_inbound(*, now: datetime | None = None, limit: int | None = None) -> int:
    """
    Responde mensajes que Graph trajo pero el webhook no procesó.

    Si Meta no entrega un webhook (o el proceso estaba reiniciando), el sync
    igual ve el mensaje como source=graph. Sin este catch-up, queda guardado
    en fb_messages pero nadie llama run_agent().
    """
    init_tables()
    now = now or datetime.now(timezone.utc)
    cap = limit or CATCHUP_LIMIT
    with legacy.db() as c:
        rows = c.execute(
            """SELECT psid,
                      MAX(CASE WHEN direction='in' THEN created_at ELSE NULL END) last_in,
                      MAX(CASE WHEN direction='out' THEN created_at ELSE NULL END) last_out
               FROM fb_messages
               WHERE psid IS NOT NULL AND psid != ''
               GROUP BY psid
               HAVING last_in IS NOT NULL
               ORDER BY last_in DESC
               LIMIT 80"""
        ).fetchall()

    sent = 0
    for row in rows:
        if sent >= cap:
            break
        psid = str(row["psid"] or "")
        last_in = _parse_time(row["last_in"])
        last_out = _parse_time(row["last_out"])
        if not last_in:
            continue
        age = (now - last_in).total_seconds()
        if age < CATCHUP_MIN_AGE_SEC or age > CATCHUP_MAX_AGE_SEC:
            continue
        if last_out and last_out >= last_in:
            continue
        lead = legacy.get_lead(psid) or {}
        if legacy.is_bot_muted(lead):
            continue

        with legacy.db() as c:
            pending = c.execute(
                """SELECT text, attachments, source, created_at
                   FROM fb_messages
                   WHERE psid=? AND direction='in' AND created_at >= ?
                   ORDER BY created_at ASC""",
                (psid, row["last_out"] or "0000"),
            ).fetchall()
        if not pending or not any((r["source"] or "") == "graph" for r in pending):
            continue
        texts = [(r["text"] or "").strip() for r in pending if (r["text"] or "").strip()]
        if not texts:
            continue
        text = " | ".join(texts)
        try:
            from .flow import run_agent

            reply = (run_agent(psid, text, []) or "").strip()
            if not reply:
                continue
            time.sleep(max(4.0, CATCHUP_MIN_AGE_SEC - age, 0))
            if legacy.send_msg(psid, reply):
                sent += 1
                log.warning("fb catchup OUT %s | %s", psid, reply[:120])
        except Exception:
            log.exception("fb catchup psid=%s", psid)
    return sent


def retry_failed_sends(*, limit: int = 10) -> int:
    """Reintentar mensajes que el bot genero pero send_msg() fallo a enviar.
    
    Bug real: send_msg() puede fallar por red/timeout/API sin ser reintentado.
    El mensaje se queda en memoria local pero nunca llega al cliente. Esta
    funcion busca leads recientes con respuesta bot visible pero sin que esa
    respuesta haya salido a Facebook, y lo intenta de nuevo.
    """
    from .flow import run_agent
    
    init_tables()
    now = datetime.now(timezone.utc)
    
    retried = 0
    for psid in list(legacy._last_sent.keys())[:limit]:
        if retried >= limit:
            break
        try:
            lead = legacy.get_lead(psid)
            if not lead:
                continue
            last_bot_reply = legacy._last_sent.get(psid) or ""
            last_fb_out = lead.get("last_outbound") or ""
            
            if last_bot_reply and last_bot_reply.strip() != last_fb_out.strip():
                if legacy.send_msg(psid, last_bot_reply):
                    retried += 1
                    log.warning("fb retry OUT %s | %s", psid, last_bot_reply[:120])
        except Exception:
            log.exception("fb retry psid=%s", psid)
    return retried


def live_stats() -> dict[str, Any]:
    init_tables()
    with legacy.db() as c:
        total = c.execute("SELECT COUNT(*) n FROM fb_messages").fetchone()["n"]
        inbound = c.execute(
            "SELECT COUNT(*) n FROM fb_messages WHERE direction='in'"
        ).fetchone()["n"]
        outbound = c.execute(
            "SELECT COUNT(*) n FROM fb_messages WHERE direction='out'"
        ).fetchone()["n"]
        convs = c.execute(
            "SELECT COUNT(DISTINCT COALESCE(NULLIF(psid,''), conversation_id)) n FROM fb_messages"
        ).fetchone()["n"]
        events = c.execute("SELECT COUNT(*) n FROM fb_events").fetchone()["n"]
        last = c.execute(
            "SELECT created_at, text, direction, sender_name, psid FROM fb_messages "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        hours = c.execute(
            """SELECT substr(created_at, 12, 2) h, COUNT(*) n
               FROM fb_messages WHERE length(created_at) >= 13
               GROUP BY h ORDER BY h"""
        ).fetchall()
        days = c.execute(
            """SELECT substr(created_at, 1, 10) d, COUNT(*) n
               FROM fb_messages WHERE length(created_at) >= 10
               GROUP BY d ORDER BY d DESC LIMIT 14"""
        ).fetchall()
        types = c.execute(
            "SELECT event_type, COUNT(*) n FROM fb_events GROUP BY event_type"
        ).fetchall()
        recent = c.execute(
            """SELECT mid, psid, sender_name, sender_id, direction, text, created_at, source
               FROM fb_messages ORDER BY created_at DESC LIMIT 80"""
        ).fetchall()
        top = c.execute(
            """SELECT psid, COUNT(*) n,
                      MAX(created_at) last_at,
                      SUM(CASE WHEN direction='in' THEN 1 ELSE 0 END) inbound
               FROM fb_messages
               WHERE psid IS NOT NULL AND psid != ''
               GROUP BY psid ORDER BY last_at DESC LIMIT 30"""
        ).fetchall()

    last_d = dict(last) if last else None
    if last_d:
        last_d["text"] = (last_d.get("text") or "")[:180]
    return {
        "stats": {
            "messages": total,
            "inbound": inbound,
            "outbound": outbound,
            "conversations": convs,
            "events": events,
            "leads_local": len(legacy.list_leads()),
            "synced_at": legacy.get_setting("fb_sync_at", ""),
        },
        "hours": {r["h"]: r["n"] for r in hours if r["h"]},
        "days": [{"day": r["d"], "n": r["n"]} for r in days],
        "event_types": {r["event_type"]: r["n"] for r in types},
        "last": last_d,
        "recent": [dict(r) for r in recent],
        "conversations": [dict(r) for r in top],
        "messenger": legacy.messenger_status(),
    }


def conversation_thread(psid: str, limit: int = 200) -> list[dict[str, Any]]:
    init_tables()
    with legacy.db() as c:
        rows = c.execute(
            """SELECT mid, psid, sender_id, sender_name, direction, text,
                      created_at, attachments, source
               FROM fb_messages WHERE psid=?
               ORDER BY created_at ASC LIMIT ?""",
            (psid, limit),
        ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        try:
            item["attachments"] = json.loads(item.get("attachments") or "[]")
        except Exception:
            item["attachments"] = []
        out.append(item)
    return out


def start_sync_loop() -> None:
    global _loop_started
    if _loop_started:
        return
    _loop_started = True

    def _run() -> None:
        time.sleep(4)
        while True:
            try:
                got = sync_from_graph()
                log.info(
                    "fb sync conv=%s msg=%s err=%s",
                    got.get("conversations"),
                    got.get("messages"),
                    got.get("errors"),
                )
            except Exception:
                log.exception("fb sync loop")
            time.sleep(max(15, SYNC_INTERVAL_SEC))

    threading.Thread(target=_run, name="fb-inbox-sync", daemon=True).start()
