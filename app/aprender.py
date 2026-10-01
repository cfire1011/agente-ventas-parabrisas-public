"""
Log al paso + aliases que el bot aprende solo.

aprender.txt  — una linea por evento (docker lo monta)
aliases_aprendidos.json — make/modelo que Safelite no trago y luego se corrigio
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock

log = logging.getLogger("aprender")

BASE = Path(__file__).resolve().parent.parent
APRENDER = BASE / "aprender.txt"
ALIASES = BASE / "aliases_aprendidos.json"
_lock = Lock()

_SKIP_SAID = re.compile(
    r"(?i)^(pickup|pick|sierra|silverado|f series|civic|accord|camry|corolla|1500|2500|3500)$"
)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def rec(kind: str, psid: str = "", detail: str = "", lead: dict | None = None) -> None:
    auto = ""
    if lead:
        auto = " ".join(
            x for x in [str(lead.get("year") or ""), lead.get("make") or "", lead.get("model") or ""]
            if x
        ).strip()
        if not detail:
            detail = f"{auto} zip={lead.get('zip') or '-'}"
    line = f"[{_now()}] {kind} psid={psid or '-'} {detail}".strip()
    try:
        APRENDER.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with open(APRENDER, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        log.exception("aprender rec")


def load_aliases() -> dict[str, dict[str, str]]:
    try:
        if not ALIASES.exists():
            return {}
        data = json.loads(ALIASES.read_text(encoding="utf-8") or "{}")
        by = data.get("by_make") if isinstance(data, dict) else {}
        return by if isinstance(by, dict) else {}
    except Exception:
        log.exception("load aliases")
        return {}


def remember_alias(make: str, said: str, real: str, psid: str = "") -> bool:
    mk, sd, rl = (make or "").strip(), (said or "").strip(), (real or "").strip()
    if not mk or not sd or not rl:
        return False
    if _key(sd) == _key(rl):
        return False
    if _SKIP_SAID.match(sd.strip()) or len(_key(sd)) < 3:
        return False
    mk_k, sd_k = _key(mk), _key(sd)
    if not mk_k or not sd_k:
        return False
    # Guardar en SQLite (nuevo sistema) — el JSON se migra al arrancar
    try:
        from .memory import remember_alias as _mem_alias
        _mem_alias(make, said, real, psid)
    except Exception:
        log.exception("memory alias")
    # También al JSON viejo (compatibilidad mientras migra)
    with _lock:
        by = load_aliases()
        bucket = by.get(mk_k) or {}
        if not isinstance(bucket, dict):
            bucket = {}
        if bucket.get(sd_k) == rl:
            return False
        bucket[sd_k] = rl
        by[mk_k] = bucket
        ALIASES.write_text(
            json.dumps({"by_make": by}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    rec("ALIAS", psid, f"{mk} | {sd} → {rl}")
    return True


def alias_attempts(make: str, model: str) -> list[str]:
    # Primero la memoria SQLite (nueva), luego el JSON viejo (compatibilidad)
    try:
        from .memory import get_alias
        learned = get_alias(make, model)
        if learned:
            return [learned]
    except Exception:
        pass
    by = load_aliases()
    val = (by.get(_key(make)) or {}).get(_key(model))
    if not val:
        return []
    return [val] if isinstance(val, str) else list(val)


# ---------------------------------------------------------------------------
# Memoria viva: frases reales → qué acción ganó (se guarda en leads.db)
# ---------------------------------------------------------------------------
_SKIP_FP = re.compile(
    r"^(ok|okay|si|sí|no|gracias|thanks|hola|hi|hello|dale|va|1|2|3|4|5)$",
    re.I,
)
_bootstrapped = False


def init_memory() -> None:
    from . import legacy

    with legacy.db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS learned_phrases (
                fp TEXT PRIMARY KEY,
                action TEXT,
                wins INTEGER DEFAULT 0,
                loss INTEGER DEFAULT 0,
                example TEXT,
                updated_at TEXT
            )"""
        )


def fingerprint(text: str) -> str:
    from .faq_extra import _norm

    t = _norm(text or "")
    t = re.sub(r"[^a-z0-9ñ ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t or _SKIP_FP.match(t):
        return ""
    if re.fullmatch(r"\d{5}", t):
        return ""
    words = t.split()
    return " ".join(words[:8])[:80]


def _gap(lead: dict | None) -> str:
    lead = lead or {}
    from . import legacy

    if not legacy._has_vehicle(lead):
        return "ask_car"
    if not legacy._lead_zip(lead):
        return "ask_zip"
    if not legacy._already_showed_slots(lead):
        return "offer_slots"
    return "fb_call"


def remember_phrase(text: str, action: str, *, win: bool, example: str = "") -> None:
    fp = fingerprint(text)
    if not fp or not action:
        return
    init_memory()
    from . import legacy

    col = "wins" if win else "loss"
    with legacy.db() as c:
        c.execute(
            f"""INSERT INTO learned_phrases(fp, action, wins, loss, example, updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(fp) DO UPDATE SET
                    action=excluded.action,
                    {col}={col}+1,
                    example=COALESCE(NULLIF(excluded.example,''), learned_phrases.example),
                    updated_at=excluded.updated_at
            """,
            (fp, action, 1 if win else 0, 0 if win else 1, (example or text)[:160], _now()),
        )


def lookup_phrase(text: str) -> dict | None:
    fp = fingerprint(text)
    if not fp:
        return None
    init_memory()
    from . import legacy

    with legacy.db() as c:
        r = c.execute(
            "SELECT fp, action, wins, loss, example FROM learned_phrases WHERE fp=?",
            (fp,),
        ).fetchone()
    return dict(r) if r else None


def suggest_reply(lead: dict, text: str) -> str | None:
    """Si esta frase ya ganó de verdad, reusa la acción ganadora (no texto muerto)."""
    row = lookup_phrase(text)
    if not row:
        return None
    wins = int(row.get("wins") or 0)
    loss = int(row.get("loss") or 0)
    if wins < 2:
        return None
    if wins <= loss and wins < 5:
        return None
    return render_action(row.get("action") or "ask_car", lead, text)


def render_action(action: str, lead: dict, text: str) -> str:
    from . import legacy

    lang = legacy._client_lang(lead, text, psid=(lead or {}).get("psid"))
    action = (action or "ask_car").strip()
    if action == "negocio":
        return legacy._faq_extra_reply(lead, "informacion del negocio", lang) or legacy._next_missing_ask(lead, text=text)
    if action == "fb_call":
        return legacy._fb_call_line(lead, text)
    if action == "price":
        return legacy._reply_price_short(lead, text)
    if action == "ask_zip":
        if legacy._has_vehicle(lead):
            return legacy._ask_zip_now(lead, text)
        return legacy._next_missing_ask(lead, text=text)
    if action == "offer_slots":
        if legacy._slots_ready(lead):
            return legacy._t(
                lang,
                f"Para tu {legacy._vehicle_label(lead)} ya puedo sacar horarios. {legacy.FB_CALL_ES}",
                f"For your {legacy._vehicle_label(lead)} I can pull times now. {legacy.FB_CALL_EN}",
            )
        return legacy._next_missing_ask(lead, text=text)
    return legacy._next_missing_ask(lead, text=text)


def observe(user_text: str, before: dict | None, after: dict | None) -> None:
    """Cada turno: si avanzamos (auto/zip/cita) esa frase gana. Si no, pierde un poco."""
    if not fingerprint(user_text or ""):
        return
    before, after = before or {}, after or {}
    from . import legacy

    progressed = (
        (not legacy._has_vehicle(before) and legacy._has_vehicle(after))
        or (not legacy._lead_zip(before) and bool(legacy._lead_zip(after)))
        or (not before.get("slot_id") and bool(after.get("slot_id")))
        or (not before.get("slots_json") and bool(after.get("slots_json")))
    )
    action = _gap(before)
    # Si el texto ya es FAQ conocida, guarda esa intención.
    from .faq_extra import detectar

    intent = detectar(user_text) or action
    remember_phrase(user_text, intent, win=bool(progressed), example=user_text)


def bootstrap_from_leads() -> int:
    """Lee el historial real y puntúa frases. Se puede correr mil veces."""
    global _bootstrapped
    init_memory()
    from . import legacy

    n = 0
    for lead in legacy.list_leads():
        try:
            hist = json.loads(lead.get("history") or "[]")
        except Exception:
            hist = []
        users = [str(m.get("content") or "") for m in hist if (m.get("role") or "") == "user"]
        won = bool(legacy._has_vehicle(lead) or legacy._lead_zip(lead) or lead.get("slots_json"))
        for u in users[:6]:
            if not fingerprint(u):
                continue
            from .faq_extra import detectar

            action = detectar(u) or ("ask_car" if not legacy._has_vehicle(lead) else _gap(lead))
            remember_phrase(u, action, win=won, example=u)
            n += 1
        # aliases sucios → modelo real
        if users and lead.get("make") and lead.get("model"):
            from .legacy import extract_vehicle_from_text

            for said in users[:8]:
                if not said or len(said) > 48 or len(said.split()) > 5:
                    continue
                if detectar(said):
                    continue
                veh = extract_vehicle_from_text(said) or {}
                dirty = (veh.get("model") or "").strip()
                real = str(lead.get("model") or "").strip()
                if dirty and real and dirty.lower() != real.lower():
                    remember_alias(str(lead.get("make")), dirty, real, psid=str(lead.get("psid") or ""))
    _bootstrapped = True
    rec("BOOTSTRAP", "", f"frases={n}")
    return n


def ensure_bootstrapped() -> None:
    if _bootstrapped:
        return
    try:
        bootstrap_from_leads()
    except Exception:
        log.exception("bootstrap")


# ---------------------------------------------------------------------------
# Aprendizaje desde el inbox REAL de Facebook (fb_messages) — conversaciones
# completas, de principio a fin, incluyendo lo que el humano ("self") contestó
# manualmente cuando tomó el chat. Esto es la fuente más rica: no solo lo que
# el bot ya sabía, sino lo que el negocio realmente respondió a cada cliente.
# ---------------------------------------------------------------------------
_PHONE_RE = re.compile(r"\b\d{3}[\s\-\.]?\d{3}[\s\-\.]?\d{4}\b")
_MAX_HUMAN_REPLY_LEN = 260
_MIN_HUMAN_REPLY_LEN = 8


def init_human_replies_table() -> None:
    from . import legacy

    with legacy.db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS human_replies (
                fp TEXT PRIMARY KEY,
                reply TEXT,
                uses INTEGER DEFAULT 0,
                example_question TEXT,
                updated_at TEXT
            )"""
        )


_CONTEXT_SENSITIVE_RE = re.compile(
    r"\b(c[oó]digo postal|zip ?code|\bzip\b|domicilio|n[uú]mero de tel[eé]fono"
    r"|n[uú]mero de serie|\bvin\b|whatsapp|su tel[eé]fono)\b",
    re.I,
)


def _looks_generalizable(reply: str) -> bool:
    """No aprender precios/telefonos/direcciones especificos de UN cliente,
    ni respuestas que dependan de datos previos de ESA conversacion
    (zip, telefono, VIN) — esas ya las maneja el flujo dedicado y reusarlas
    fuera de contexto puede confundir a otro cliente."""
    r = (reply or "").strip()
    if not (_MIN_HUMAN_REPLY_LEN <= len(r) <= _MAX_HUMAN_REPLY_LEN):
        return False
    if _PHONE_RE.search(r):
        return False
    if re.search(r"\$\s*\d", r):
        return False
    if re.search(r"\b\d{4,}\b", r):
        return False
    if _CONTEXT_SENSITIVE_RE.search(r):
        return False
    return True


def remember_human_reply(question: str, reply: str) -> bool:
    """Guarda la respuesta REAL de un humano a una pregunta que el bot no
    tenia cubierta, para poder reusarla despues (ver human_answer_for)."""
    fp = fingerprint(question)
    if not fp or not _looks_generalizable(reply):
        return False
    init_human_replies_table()
    from . import legacy

    with legacy.db() as c:
        c.execute(
            """INSERT INTO human_replies(fp, reply, uses, example_question, updated_at)
                VALUES(?,?,1,?,?)
                ON CONFLICT(fp) DO UPDATE SET
                    reply=excluded.reply,
                    uses=uses+1,
                    updated_at=excluded.updated_at
            """,
            (fp, reply.strip(), question[:160], _now()),
        )
    return True


def human_answer_for(text: str) -> str | None:
    """Si un cliente pregunta algo que un humano ya contesto antes (>=2 veces
    visto) y el bot no tiene regla para eso, reusa esa respuesta probada."""
    fp = fingerprint(text)
    if not fp:
        return None
    init_human_replies_table()
    from . import legacy

    with legacy.db() as c:
        row = c.execute(
            "SELECT reply, uses FROM human_replies WHERE fp=?", (fp,)
        ).fetchone()
    if not row or int(row["uses"] or 0) < 3:
        return None
    return row["reply"]


def learn_from_inbox(days: int = 7, sync_first: bool = True) -> dict:
    """
    Aprende de TODA la conversacion real de Facebook (fb_messages) de los
    ultimos `days` dias: cliente a cliente, mensaje a mensaje, de principio
    a fin — incluyendo lo que el humano ("self") contesto manualmente.

    - Frases del cliente -> que accion siguio funcionando (remember_phrase).
    - Apodos de modelo -> modelo real (remember_alias), usando el auto final
      confirmado del lead.
    - Preguntas SIN regla propia -> la respuesta real que dio un humano
      (remember_human_reply), para reusarla despues con human_answer_for().
    """
    from . import legacy
    from .faq_extra import detectar

    stats = {
        "conversations": 0,
        "messages_seen": 0,
        "phrases_learned": 0,
        "aliases_learned": 0,
        "human_answers_learned": 0,
        "errors": 0,
    }

    if sync_first:
        try:
            from . import fb_inbox

            fb_inbox.sync_from_graph(max_conversations=600)
        except Exception:
            log.exception("sync_from_graph antes de aprender")

    init_memory()
    init_human_replies_table()

    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    with legacy.db() as c:
        rows = c.execute(
            """SELECT psid, direction, text, created_at
               FROM fb_messages
               WHERE psid != '' AND psid IS NOT NULL
                 AND created_at >= ?
               ORDER BY psid, created_at ASC""",
            (cutoff,),
        ).fetchall()

    by_psid: dict[str, list[dict]] = {}
    for r in rows:
        by_psid.setdefault(r["psid"], []).append(dict(r))

    for psid, turns in by_psid.items():
        if not turns:
            continue
        stats["conversations"] += 1
        stats["messages_seen"] += len(turns)
        try:
            lead = legacy.get_lead(psid) or {}
        except Exception:
            lead = {}
        final_make = str(lead.get("make") or "").strip()
        final_model = str(lead.get("model") or "").strip()
        won = bool(
            legacy._has_vehicle(lead) or legacy._lead_zip(lead) or lead.get("slots_json")
        )

        # Estado acumulado reconstruido mensaje a mensaje (para saber si
        # ESTE turno hizo avanzar la conversacion: auto o zip nuevos).
        seen_year = seen_make = seen_model = ""
        seen_zip = ""
        for i, turn in enumerate(turns):
            text = str(turn.get("text") or "").strip()
            if not text or turn.get("direction") != "in":
                continue
            before_ok_veh = bool(seen_year and seen_make and seen_model)
            before_ok_zip = bool(seen_zip)
            try:
                veh = legacy.extract_vehicle_from_text(text) or {}
            except Exception:
                veh = {}
            seen_year = seen_year or veh.get("year") or ""
            seen_make = seen_make or veh.get("make") or ""
            seen_model = seen_model or veh.get("model") or ""
            try:
                from .zip_norm import extract_zip as _zip_extract

                z = _zip_extract(text) or ""
            except Exception:
                z = ""
            seen_zip = seen_zip or z
            after_ok_veh = bool(seen_year and seen_make and seen_model)
            after_ok_zip = bool(seen_zip)
            progressed = (not before_ok_veh and after_ok_veh) or (
                not before_ok_zip and after_ok_zip
            )

            try:
                intent = detectar(text)
                action = intent or _gap(
                    {
                        "year": seen_year,
                        "make": seen_make,
                        "model": seen_model,
                        "zip": seen_zip,
                    }
                )
                remember_phrase(text, action, win=bool(progressed or won), example=text)
                stats["phrases_learned"] += 1
            except Exception:
                stats["errors"] += 1

            # Alias: lo que dijo vs modelo real confirmado en el lead.
            if final_make and final_model and veh.get("model"):
                dirty = str(veh.get("model") or "").strip()
                if dirty and dirty.lower() != final_model.lower() and len(text.split()) <= 6:
                    try:
                        if remember_alias(final_make, dirty, final_model, psid=psid):
                            stats["aliases_learned"] += 1
                    except Exception:
                        stats["errors"] += 1

            # Respuesta real del humano/bot inmediatamente despues, SOLO si
            # esta pregunta no tiene ya una regla de FAQ conocida (ahi esta
            # el hueco real que vale la pena aprender).
            if intent:
                continue
            nxt = turns[i + 1] if i + 1 < len(turns) else None
            if nxt and nxt.get("direction") == "out":
                reply_text = str(nxt.get("text") or "").strip()
                if reply_text:
                    try:
                        if remember_human_reply(text, reply_text):
                            stats["human_answers_learned"] += 1
                    except Exception:
                        stats["errors"] += 1

    rec("LEARN_INBOX", "", json.dumps(stats, ensure_ascii=False))
    return stats

