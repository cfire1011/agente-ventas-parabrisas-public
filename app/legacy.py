"""
Agente de ventas Messenger + reserva Safelite.
Un solo archivo: chatea, pide solo lo necesario, agenda y guarda en citas.txt
"""
from __future__ import annotations

import os
import re
import json
import hmac
import hashlib
import base64
import sqlite3
import logging
import asyncio
from datetime import date, timedelta, datetime
from pathlib import Path
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from collections import defaultdict
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, Query, BackgroundTasks, Header, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse

from safelite import (
    safelite_zip,
    safelite_vehicle,
    safelite_parts,
    safelite_slots,
    safelite_shop_slots,
    safelite_book,
    flatten_slots,
)

from app.vehicle_norm import normalize_vehicle
from app.zip_norm import extract_zip as zip_extract, resolve_zip
from app.vehicle_norm import MAKE_ALIASES as _MAKE_ALIASES
from app.faq_extra import detectar as _faq_extra_detectar
from app.vehicle_disambig import (
    candidatos_mostrados as _veh_candidatos_mostrados,
    parse_respuesta as _veh_parse_respuesta,
    pregunta as _veh_pregunta,
)
from app.slot_choice import (
    MAX_OPCIONES as SLOT_MAX_OPCIONES,
    es_flexible as _es_flexible,
    es_afirmacion as _es_afirmacion,
    opciones_mostradas as _opciones_mostradas,
    parse_natural as _slot_parse_natural,
    lista_numerada as _lista_numerada,
    parse_choice as _parse_num_choice,
    ventanas_unicas as _ventanas_unicas,
)
from app.llm_extract import (
    extract_location as llm_extract_location,
    extract_vehicle as llm_extract_vehicle,
)
from app import llm_status

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
# app.log tenia el PAGE_ACCESS_TOKEN en texto plano (va en la query string de
# la Graph API). Se instala despues de load_dotenv para conocer los valores.
from app.log_filter import instalar as _instalar_filtro_secretos  # noqa: E402

_instalar_filtro_secretos()
log = logging.getLogger("agente")

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "leads.db"
CITAS = BASE / "citas.txt"  # notepad de citas activas / log
CITAS_DONE = BASE / "citas_finalizadas.txt"  # archivo aparte de citas terminadas
DASH_HTML = BASE / "dashboard.html"
NM_FILE = BASE / "nm.txt"       # telefonos para la web (1 por linea)
EMAIL_FILE = BASE / "email.txt" # emails para la web (1 por linea)

GRAPH = "https://graph.facebook.com/v21.0"

PRECIO = os.getenv("PRECIO", "210")
NEGOCIO = os.getenv("NEGOCIO_NOMBRE", "Parabrisas solution")
DASH_USER = os.getenv("DASHBOARD_USER", "admin")
DASH_PASS = os.getenv("DASHBOARD_PASSWORD", "")
STAFF_USER = os.getenv("STAFF_USER", "chat")
STAFF_PASS = os.getenv("STAFF_PASSWORD", "")
# Cierre al cliente: NUNCA pedir su número. Llamada por Facebook.
FB_CALL_ES = "Puedes llamarnos por Facebook, por aquí mismo."
FB_CALL_EN = "You can call us on Facebook, right here."
_ASK_CLIENT_PHONE_RX = re.compile(
    r"(?i)[^.?!\n]*("
    r"dame tu n[uú]mero( de tel[eé]fono)?"
    r"|p[aá]same tu tel[eé]fono"
    r"|tu n[uú]mero de tel[eé]fono"
    r"|para marcarte"
    r"|marcarte ahora"
    r"|nombre y tel[eé]fono"
    r"|send (me )?(your )?phone"
    r"|what('?s| is) your (phone|number)"
    r"|your phone number"
    r")[^.?!\n]*[.?!]?"
)
# token -> {user, role}  (reinicia al reiniciar el bot)
_dash_sessions: dict[str, dict] = {}

app = FastAPI(title=NEGOCIO)
_executor = ThreadPoolExecutor(max_workers=4)
_pending: dict[str, dict] = {}
_seq: dict[str, int] = defaultdict(int)
_lock = Lock()
DEBOUNCE = 0.6

# ---------------------------------------------------------------------------
# DB mínima
# ---------------------------------------------------------------------------
@contextmanager
def db():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.text_factory = lambda b: b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else b
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS leads (
                psid TEXT PRIMARY KEY,
                name TEXT,
                phone TEXT,
                year TEXT,
                make TEXT,
                model TEXT,
                style TEXT,
                glass TEXT DEFAULT 'Windshield',
                zip TEXT,
                address TEXT,
                city TEXT,
                state TEXT,
                slot_date TEXT,
                slot_time TEXT,
                work_order TEXT,
                portal TEXT,
                stage TEXT DEFAULT 'nuevo',
                history TEXT DEFAULT '[]',
                admin_note TEXT,
                updated_at TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                value TEXT NOT NULL UNIQUE,
                status TEXT DEFAULT 'free',
                lead_psid TEXT,
                work_order TEXT,
                assigned_at TEXT,
                freed_at TEXT
            )"""
        )
        # migracion suave
        cols = {r[1] for r in c.execute("PRAGMA table_info(leads)").fetchall()}
        for col, typ in (
            ("admin_note", "TEXT"),
            ("slot_id", "TEXT"),
            ("slots_json", "TEXT"),
            ("pool_phone", "TEXT"),
            ("pool_email", "TEXT"),
            ("client_phone", "TEXT"),
            ("service_mode", "TEXT"),  # domicilio | sucursal
            ("slots_fetched_at", "TEXT"),  # cuando se pidio live a Safelite
            ("address_verified", "TEXT"),  # ok | fail | partial | ""
            ("address_formatted", "TEXT"),  # como la vio geocoder gratis
            ("lat", "TEXT"),
            ("lng", "TEXT"),
            ("bot_muted", "TEXT"),  # "1" = no responder mas (paso a humano)
            ("slots_error", "TEXT"),  # ultimo error al pedir disponibilidad
            ("finished_at", "TEXT"),  # cuando se marco finalizado
            ("booked_at", "TEXT"),  # cuando se genero el WO
            # "1" = recap listo, esperando SI del cliente (= Continue en safelite.com/fmg/schedule)
            ("confirm_pending", "TEXT"),
            # precio ya dicho al cliente (se BLOQUEA; no subir de 210 a 260 etc.)
            ("quoted_price", "TEXT"),
            ("last_inbound_at", "TEXT"),  # ultimo mensaje del cliente
            ("nudge_sent", "TEXT"),  # auto / horarios (recordatorios)
            ("nudge_at", "TEXT"),
            ("last_outbound", "TEXT"),  # ultimo texto POST a Messenger (anti-duplicado)
        ):
            if col not in cols:
                c.execute(f"ALTER TABLE leads ADD COLUMN {col} {typ}")
        sync_pool_files()
    try:
        from .safelite_acceso import init_acceso_table
        init_acceso_table()
    except Exception:
        pass
    try:
        from .fb_inbox import init_tables
        init_tables()
    except Exception:
        pass
    try:
        from .aprender import init_memory, ensure_bootstrapped
        init_memory()
        ensure_bootstrapped()
    except Exception:
        pass


def get_lead(psid: str) -> dict:
    with db() as c:
        r = c.execute("SELECT * FROM leads WHERE psid=?", (psid,)).fetchone()
        return dict(r) if r else {}


_LEAD_COLS = {
    "name", "phone", "year", "make", "model", "style", "glass", "zip",
    "address", "city", "state", "slot_date", "slot_time", "work_order",
    "portal", "stage", "history", "admin_note", "slot_id", "slots_json",
    "pool_phone", "pool_email", "client_phone", "service_mode", "slots_fetched_at",
    "address_verified", "address_formatted", "lat", "lng",
    "bot_muted", "slots_error", "finished_at", "booked_at", "confirm_pending",
    "quoted_price", "last_inbound_at", "nudge_sent", "nudge_at",
    "last_outbound",
}


def sync_pool_files():
    """Carga nm.txt y email.txt a la tabla pool (sin borrar los in_use)."""
    def load_file(path: Path, kind: str):
        if not path.exists():
            return
        vals = []
        for line in path.read_text(encoding="utf-8").splitlines():
            v = line.strip()
            if not v or v.startswith("#"):
                continue
            if kind == "phone":
                v = re.sub(r"\D", "", v)
                if len(v) == 11 and v.startswith("1"):
                    v = v[1:]
            vals.append(v)
        with db() as c:
            for v in vals:
                c.execute(
                    "INSERT OR IGNORE INTO pool(kind, value, status) VALUES(?,?, 'free')",
                    (kind, v),
                )

    load_file(NM_FILE, "phone")
    load_file(EMAIL_FILE, "email")


def assign_pool(kind: str, psid: str) -> str | None:
    """Toma 1 contacto free, lo marca in_use. No se reusa hasta free_pool()."""
    sync_pool_files()
    now = datetime.utcnow().isoformat()
    with db() as c:
        # Si este lead ya tiene uno asignado, reusar el mismo (no gastar otro)
        row = c.execute(
            "SELECT id, value FROM pool WHERE kind=? AND lead_psid=? AND status='in_use' "
            "ORDER BY id ASC LIMIT 1",
            (kind, psid),
        ).fetchone()
        if row:
            return row["value"]
        row = c.execute(
            "SELECT id, value FROM pool WHERE kind=? AND status='free' ORDER BY id ASC LIMIT 1",
            (kind,),
        ).fetchone()
        if not row:
            return None
        c.execute(
            "UPDATE pool SET status='in_use', lead_psid=?, assigned_at=?, freed_at=NULL WHERE id=?",
            (psid, now, row["id"]),
        )
        return row["value"]


def free_pool_for_lead(psid: str, work_order: str | None = None):
    """Libera phone/email del pool SOLO cuando la cita finaliza por completo (o falla el book)."""
    now = datetime.utcnow().isoformat()
    with db() as c:
        if work_order:
            c.execute(
                "UPDATE pool SET status='free', lead_psid=NULL, work_order=NULL, freed_at=? "
                "WHERE lead_psid=? OR work_order=?",
                (now, psid, work_order),
            )
        else:
            c.execute(
                "UPDATE pool SET status='free', lead_psid=NULL, work_order=NULL, freed_at=? "
                "WHERE lead_psid=?",
                (now, psid),
            )


def bind_pool_work_order(psid: str, work_order: str):
    with db() as c:
        c.execute(
            "UPDATE pool SET work_order=? WHERE lead_psid=? AND status='in_use'",
            (work_order, psid),
        )


def pool_status() -> dict:
    """Resumen del pool nm/email para admin."""
    sync_pool_files()
    with db() as c:
        rows = c.execute(
            "SELECT kind, value, status, lead_psid, work_order, assigned_at FROM pool ORDER BY kind, id"
        ).fetchall()
    items = [dict(r) for r in rows]
    free_p = sum(1 for i in items if i["kind"] == "phone" and i["status"] == "free")
    free_e = sum(1 for i in items if i["kind"] == "email" and i["status"] == "free")
    used_p = sum(1 for i in items if i["kind"] == "phone" and i["status"] == "in_use")
    used_e = sum(1 for i in items if i["kind"] == "email" and i["status"] == "in_use")
    return {
        "items": items,
        "free_phones": free_p,
        "free_emails": free_e,
        "used_phones": used_p,
        "used_emails": used_e,
    }


def upsert_lead(psid: str, **fields):
    lead = get_lead(psid)
    now = datetime.utcnow().isoformat()
    if not lead:
        with db() as c:
            c.execute(
                "INSERT INTO leads (psid, updated_at) VALUES (?,?)", (psid, now)
            )
        lead = get_lead(psid)
    if fields:
        cols = []
        vals = []
        for k, v in fields.items():
            if k not in _LEAD_COLS:
                continue
            if v is None:
                continue
            cols.append(f"{k}=?")
            vals.append(v)
        if cols:
            cols.append("updated_at=?")
            vals.append(now)
            vals.append(psid)
            with db() as c:
                c.execute(f"UPDATE leads SET {', '.join(cols)} WHERE psid=?", vals)
    return get_lead(psid)


def is_real_messenger_psid(psid: str | None) -> bool:
    """Solo PSIDs oficiales de Meta/Messenger (solo digitos, largos).

    Filtra basura de tests: seller_test, t1, emp1, test_geocode_*, etc.
    """
    s = str(psid or "").strip()
    return bool(re.fullmatch(r"\d{10,}", s))


def list_leads(official_only: bool = True) -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT * FROM leads ORDER BY datetime(updated_at) DESC"
        ).fetchall()
        out = [dict(r) for r in rows]
    if official_only:
        out = [r for r in out if is_real_messenger_psid(r.get("psid"))]
    return out


def get_setting(key: str, default: str = "") -> str:
    try:
        with db() as c:
            r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return r["value"] if r and r["value"] is not None else default
    except Exception:
        return default


def set_setting(key: str, value: str):
    try:
        with db() as c:
            c.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
    except Exception as e:
        log.warning("set_setting %s: %s", key, e)


def messenger_status() -> dict:
    tok = _token()
    if not tok:
        return {"ok": False, "pagina": None, "error": "PAGE_ACCESS_TOKEN vacio"}
    try:
        # debug_token es la forma confiable (no exige pages_read_engagement)
        r = httpx.get(
            f"{GRAPH}/debug_token",
            params=_graph_params(input_token=tok),
            timeout=12,
            trust_env=False,
        )
        # si falla por appsecret_proof, reintenta sin (token-only)
        if r.status_code >= 400 and "appsecret_proof" in r.text:
            r = httpx.get(
                f"{GRAPH}/debug_token",
                params={"input_token": tok, "access_token": tok},
                timeout=12,
                trust_env=False,
            )
        data = (r.json() or {}).get("data") or {}
        if data.get("is_valid") and data.get("type") == "PAGE":
            scopes = data.get("scopes") or []
            ok_msg = "pages_messaging" in scopes
            return {
                "ok": ok_msg,
                "pagina": f"Page {data.get('profile_id')}",
                "page_id": data.get("profile_id"),
                "scopes": scopes,
                "error": None if ok_msg else "Falta pages_messaging",
            }
        # fallback /me
        r2 = httpx.get(f"{GRAPH}/me", params=_graph_params(fields="id,name"), timeout=12, trust_env=False)
        if r2.status_code == 200:
            j = r2.json()
            return {"ok": True, "pagina": j.get("name"), "page_id": j.get("id")}
        err = (r.json() if r.status_code >= 400 else r2.json())
        return {"ok": False, "pagina": None, "error": str(err)[:160]}
    except Exception as e:
        return {"ok": False, "pagina": None, "error": str(e)}


def _cred_ok(got: str, expected: str) -> bool:
    a = (got or "").encode("utf-8")
    b = (expected or "").encode("utf-8")
    if not b or len(a) != len(b):
        hmac.compare_digest(b"xxxxxxxx", b"yyyyyyyy")
        return False
    return hmac.compare_digest(a, b)


def _require_dash(x_dashboard_token: str | None = Header(None), admin: bool = False) -> dict:
    """Exige login. Staff solo chat; admin ve todo."""
    tok = (x_dashboard_token or "").strip()
    sess = _dash_sessions.get(tok) if tok else None
    if not sess:
        raise HTTPException(status_code=401, detail="Login requerido")
    if admin and sess.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Solo admin")
    return sess


def _staff_lead_public(lead: dict | None) -> dict:
    """Quita portal / pool / notas internas para el usuario de chat."""
    if not lead:
        return {}
    hide = {
        "portal", "pool_phone", "pool_email", "work_order", "admin_note",
        "slots_json", "slot_id", "lat", "lng",
    }
    out = {k: v for k, v in lead.items() if k not in hide}
    return out


def history(psid: str) -> list:
    return json.loads(get_lead(psid).get("history") or "[]")


def append_hist(psid: str, role: str, content: str):
    h = history(psid)
    h.append({"role": role, "content": content})
    h = h[-24:]
    upsert_lead(psid, history=json.dumps(h, ensure_ascii=False))


def note(line: str):
    """Append a line to the notepad citas.txt"""
    with open(CITAS, "a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")


# ---------------------------------------------------------------------------
# Messenger (Graph API + appsecret_proof si Meta lo exige)
# ---------------------------------------------------------------------------
def _token() -> str:
    return (os.getenv("PAGE_ACCESS_TOKEN") or "").strip()


def _app_secret() -> str:
    return (os.getenv("APP_SECRET") or "").strip()


def _graph_params(**extra) -> dict:
    """Params para Graph. Si hay APP_SECRET, manda appsecret_proof (Meta 'Require App Secret')."""
    tok = _token()
    params = {"access_token": tok}
    secret = _app_secret()
    if secret and tok:
        params["appsecret_proof"] = hmac.new(
            secret.encode("utf-8"),
            tok.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    params.update({k: v for k, v in extra.items() if v is not None})
    return params


_last_sent: dict[str, str] = {}


def _norm_outbound(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())[:1200]


def _is_slots_outbound(text: str) -> bool:
    return bool(
        re.search(
            r"(?i)(cu[aá]l te sirve|n[uú]mero o d[ií]a|"
            r"horarios en (que podemos ir|taller)|lo m[aá]s pronto|"
            r"vamos a tu lugar:)",
            text or "",
        )
    )


def _same_outbound(a: str, b: str) -> bool:
    """True si es el mismo mensaje (o la misma lista de horarios con otro wording)."""
    na, nb = _norm_outbound(a), _norm_outbound(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if _is_slots_outbound(a) and _is_slots_outbound(b):
        return True
    if min(len(na), len(nb)) >= 80 and na[:80] == nb[:80]:
        return True
    return False


def should_skip_send(psid: str, text: str) -> bool:
    """NUNCA reenviar lo que YA se POSTEÓ a Messenger. No uses history:
    run_agent appendea el reply antes de send_msg y eso bloqueaba el 1er envío."""
    text = (text or "").strip()
    if not text:
        return True
    prev = _last_sent.get(str(psid)) or ""
    if not prev:
        lead = get_lead(str(psid)) or {}
        prev = (lead.get("last_outbound") or "").strip()
    return _same_outbound(prev, text)


def send_msg(psid: str, text: str, tag: str | None = None, *, _retried: bool = False) -> bool:
    if not text:
        return False
    # Solo PSIDs reales de Messenger (evita test_local)
    if not re.fullmatch(r"\d{5,}", str(psid)):
        log.info("send skip non-messenger psid=%s", psid)
        return False
    if should_skip_send(str(psid), text):
        log.warning("send skip duplicate psid=%s | %s", psid, (text or "")[:80])
        return False
    try:
        from app.human_takeover import remember_bot_send

        # Antes del POST: si Meta ecos el mensaje, no nos silenciamos solos.
        remember_bot_send(str(psid), text[:180])
        payload = {
            "recipient": {"id": str(psid)},
            "message": {"text": text[:1900]},
        }
        if tag:
            payload["messaging_type"] = "MESSAGE_TAG"
            payload["tag"] = tag
        else:
            payload["messaging_type"] = "RESPONSE"
        r = httpx.post(
            f"{GRAPH}/me/messages",
            params=_graph_params(),
            json=payload,
            timeout=20,
            trust_env=False,
        )
        if r.status_code >= 400:
            log.error("send fail %s %s", psid, r.text[:300])
            err: dict = {}
            try:
                err = (r.json() or {}).get("error") or {}
            except Exception:
                err = {}
            sub = err.get("error_subcode")
            msg = str(err.get("message") or "")
            if (
                not _retried
                and not tag
                and (
                    sub == 2018278
                    or "período permitido" in msg
                    or "period allowed" in msg.lower()
                )
            ):
                log.warning("24h window psid=%s — reintento HUMAN_AGENT", psid)
                return send_msg(psid, text, tag="HUMAN_AGENT", _retried=True)
            return False
        mid = None
        try:
            mid = (r.json() or {}).get("message_id")
        except Exception:
            mid = None
        remember_bot_send(str(psid), text[:180], mid=mid)
        out = text[:1900]
        _last_sent[str(psid)] = out
        try:
            upsert_lead(str(psid), last_outbound=out)
        except Exception:
            pass
        return True
    except Exception as e:
        log.error("send exc %s", e)
        return False


def typing(psid: str, on: bool = True):
    if not re.fullmatch(r"\d{5,}", str(psid)):
        return
    try:
        httpx.post(
            f"{GRAPH}/me/messages",
            params=_graph_params(),
            json={
                "recipient": {"id": str(psid)},
                "sender_action": "typing_on" if on else "typing_off",
            },
            timeout=10,
            trust_env=False,
        )
    except Exception:
        pass


def user_name(psid: str) -> str | None:
    if not re.fullmatch(r"\d{5,}", str(psid)):
        return None
    try:
        r = httpx.get(
            f"{GRAPH}/{psid}",
            params=_graph_params(fields="first_name"),
            timeout=10,
            trust_env=False,
        )
        if r.status_code >= 400:
            return None
        return r.json().get("first_name")
    except Exception:
        return None


def parse_signed_request(signed_request: str, secret: str) -> dict | None:
    """Decodifica signed_request de Meta (data deletion / login)."""
    try:
        encoded_sig, payload = signed_request.split(".", 1)
        def b64(s: str) -> bytes:
            s += "=" * (-len(s) % 4)
            return base64.urlsafe_b64decode(s.encode("utf-8"))
        sig = b64(encoded_sig)
        data = json.loads(b64(payload))
        expected = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        return data
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Validacion de direccion (Google Maps + fallbacks) — sin dir buena, la API
# de slots/book no agarra bien la ruta. Safelite API vive en safelite.py
# ---------------------------------------------------------------------------
def _maps_api_key() -> str:
    return (
        os.getenv("GOOGLE_MAPS_API_KEY")
        or os.getenv("GOOGLE_GEOCODING_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
        or ""
    ).strip()


def _compose_address_query(lead: dict, extra: str = "") -> str:
    parts = []
    if extra and extra.strip():
        parts.append(extra.strip())
    for k in ("address", "city", "state", "zip"):
        v = (lead.get(k) or "").strip()
        if v and v not in " ".join(parts):
            parts.append(v)
    q = ", ".join(parts)
    if q and "usa" not in q.lower() and "united states" not in q.lower():
        q = q + ", USA"
    return q


def _geocode_google(query: str) -> dict | None:
    key = _maps_api_key()
    if not key or not query:
        return None
    try:
        r = httpx.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={
                "address": query,
                "key": key,
                "components": "country:US",
                "language": "en",
            },
            timeout=15,
            trust_env=False,
        )
        if r.status_code != 200:
            log.warning("google geocode http %s %s", r.status_code, r.text[:200])
            return None
        data = r.json() or {}
        status = data.get("status")
        if status != "OK" or not data.get("results"):
            log.info("google geocode status=%s query=%r", status, query[:80])
            return None
        best = data["results"][0]
        comps = {c["types"][0]: c for c in best.get("address_components") or [] if c.get("types")}
        # index by all types
        by_type: dict[str, dict] = {}
        for c in best.get("address_components") or []:
            for t in c.get("types") or []:
                by_type[t] = c

        def comp(*types, short=False):
            for t in types:
                if t in by_type:
                    return (by_type[t].get("short_name") if short else by_type[t].get("long_name")) or ""
            return ""

        street_num = comp("street_number")
        route = comp("route")
        street = f"{street_num} {route}".strip() if (street_num or route) else ""
        city = (
            comp("locality")
            or comp("sublocality")
            or comp("neighborhood")
            or comp("administrative_area_level_2")
        )
        state = comp("administrative_area_level_1", short=True)
        zipc = comp("postal_code")
        loc = (best.get("geometry") or {}).get("location") or {}
        loc_type = (best.get("geometry") or {}).get("location_type") or ""
        partial = bool(best.get("partial_match"))
        # PRECISION: necesitamos calle+numero real para Safelite mobile
        precise = bool(street_num and route and zipc) and loc_type in (
            "ROOFTOP",
            "RANGE_INTERPOLATED",
            "GEOMETRIC_CENTER",
        )
        if not street_num:
            precise = False
        return {
            "provider": "google",
            "formatted": best.get("formatted_address") or query,
            "address": street or (best.get("formatted_address") or "").split(",")[0],
            "city": city,
            "state": state,
            "zip": zipc,
            "lat": str(loc.get("lat") or ""),
            "lng": str(loc.get("lng") or ""),
            "location_type": loc_type,
            "partial_match": partial,
            "precise": precise and not partial,
            "place_id": best.get("place_id") or "",
        }
    except Exception:
        log.exception("google geocode")
        return None


def _geocode_census(query: str) -> dict | None:
    """Fallback gratis USA (Census Bureau) — sin API key."""
    if not query:
        return None
    try:
        r = httpx.get(
            "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress",
            params={
                "address": query,
                "benchmark": "Public_AR_Current",
                "format": "json",
            },
            timeout=15,
            trust_env=False,
        )
        if r.status_code != 200:
            return None
        matches = (
            ((r.json() or {}).get("result") or {}).get("addressMatches") or []
        )
        if not matches:
            return None
        m = matches[0]
        comps = m.get("addressComponents") or {}
        matched = (m.get("matchedAddress") or "").strip()
        # matchedAddress ya trae "7219 HEIRLOOM LN, INMAN, SC, 29349" — usar eso
        street = matched.split(",")[0].strip() if matched else ""
        if not street:
            name = " ".join(
                x for x in [
                    comps.get("preDirection") or "",
                    comps.get("streetName") or "",
                    comps.get("suffixType") or "",
                    comps.get("suffixDirection") or "",
                ]
                if x
            ).strip()
            # NO uses fromAddress/toAddress solos: son rangos, no el numero exacto
            street = name
        coords = m.get("coordinates") or {}
        zipc = (comps.get("zip") or "")[:5]
        has_num = bool(re.search(r"^\d{1,6}\b", street))
        return {
            "provider": "census",
            "formatted": matched or query,
            "address": street,
            "city": comps.get("city") or "",
            "state": comps.get("state") or "",
            "zip": zipc,
            "lat": str(coords.get("y") or ""),
            "lng": str(coords.get("x") or ""),
            "location_type": "census",
            "partial_match": False,
            "precise": bool(has_num and street and zipc),
            "place_id": "",
        }
    except Exception:
        log.exception("census geocode")
        return None


def _geocode_nominatim(query: str) -> dict | None:
    """Fallback OSM (rate-limit amable)."""
    if not query:
        return None
    try:
        r = httpx.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": query,
                "format": "json",
                "addressdetails": 1,
                "limit": 1,
                "countrycodes": "us",
            },
            headers={"User-Agent": "agente-ventas-parabrisas/1.0"},
            timeout=15,
            trust_env=False,
        )
        if r.status_code != 200 or not r.json():
            return None
        best = r.json()[0]
        a = best.get("address") or {}
        num = a.get("house_number") or ""
        road = a.get("road") or a.get("pedestrian") or ""
        street = f"{num} {road}".strip()
        city = a.get("city") or a.get("town") or a.get("village") or a.get("hamlet") or ""
        state = a.get("state") or ""
        # state short if possible
        zipc = (a.get("postcode") or "")[:5]
        return {
            "provider": "nominatim",
            "formatted": best.get("display_name") or query,
            "address": street or (best.get("display_name") or "").split(",")[0],
            "city": city,
            "state": state,
            "zip": zipc,
            "lat": str(best.get("lat") or ""),
            "lng": str(best.get("lon") or ""),
            "location_type": best.get("type") or "",
            "partial_match": False,
            "precise": bool(num and road and zipc),
            "place_id": str(best.get("place_id") or ""),
        }
    except Exception:
        log.exception("nominatim geocode")
        return None


def geocode_address(query: str) -> dict | None:
    """Geocodifica direccion USA.

    Orden:
    1) Google Geocoding si hay GOOGLE_MAPS_API_KEY (mas preciso; free tier de Google)
    2) Census USA (gratis, sin key)
    3) OpenStreetMap Nominatim (gratis, sin key)

    Prefiere resultados precise=True; si solo hay parcial, usa el mejor.
    """
    q = (query or "").strip()
    if not q:
        return None

    providers = []
    if _maps_api_key():
        providers.append(_geocode_google)
    providers.extend((_geocode_census, _geocode_nominatim))

    best_partial = None
    for fn in providers:
        hit = fn(q)
        if not hit or not (hit.get("zip") or hit.get("address")):
            continue
        if hit.get("precise"):
            log.info(
                "geocode ok provider=%s precise=True q=%r → %s",
                hit.get("provider"),
                q[:70],
                (hit.get("formatted") or "")[:90],
            )
            return hit
        if best_partial is None:
            best_partial = hit

    if best_partial:
        log.info(
            "geocode ok provider=%s precise=False q=%r → %s",
            best_partial.get("provider"),
            q[:70],
            (best_partial.get("formatted") or "")[:90],
        )
        return best_partial

    log.info("geocode fail q=%r", q[:80])
    return None


def is_bot_muted(lead: dict | None) -> bool:
    """Si True, el bot NO debe contestar mas (paso a agente humano)."""
    if not lead:
        return False
    if str(lead.get("bot_muted") or "").strip() in ("1", "true", "yes", "si", "sí"):
        return True
    stage = (lead.get("stage") or "").strip().lower()
    return stage in ("agente", "handoff", "humano", "humano_pendiente")


def handoff_to_human(psid: str, reason: str = "") -> str:
    """
    Un solo mensaje de cortesia y silencia el bot para ese lead.
    El humano contesta desde la Page Inbox / dashboard.
    """
    msg = "Dame un momento, te atiende alguien de aquí."
    note_txt = (
        f"[{datetime.now().isoformat()}] HANDOFF humano psid={psid} "
        f"reason={reason or '-'} — bot silenciado"
    )
    note(note_txt)
    prev = get_lead(psid) or {}
    admin = (prev.get("admin_note") or "").strip()
    admin2 = (admin + "\n" if admin else "") + note_txt
    upsert_lead(
        psid,
        stage="agente",
        bot_muted="1",
        slots_error=(reason or prev.get("slots_error") or "")[:200],
        admin_note=admin2[:2000],
    )
    log.warning("HANDOFF psid=%s reason=%s", psid, reason)
    return msg


def validate_and_normalize_address(psid: str, lead: dict, raw_hint: str = "") -> dict:
    """
    Valida la direccion del lead en mapas y la normaliza para Safelite.

    return: {
      status: 'ok' | 'need_more' | 'fail' | 'empty',
      gaps: [...],
      client_hint: str,   # que pedirle al cliente (simple)
      geo: dict|None,
    }
    """
    lead = lead or {}
    # si ya esta ok y no cambio la dir, reusar
    if (lead.get("address_verified") or "") == "ok" and lead.get("zip") and lead.get("address"):
        if not raw_hint or raw_hint.strip().lower() in (lead.get("address") or "").lower():
            return {"status": "ok", "gaps": [], "client_hint": "", "geo": None}

    # sin material de direccion
    gaps0 = _address_gaps(lead)
    has_any = bool(
        (lead.get("address") or "").strip()
        or (lead.get("zip") or "").strip()
        or (lead.get("city") or "").strip()
        or (raw_hint or "").strip()
    )
    if not has_any:
        return {
            "status": "empty",
            "gaps": [],
            "client_hint": "me pasas tu direccion?",
            "geo": None,
        }

    # si falta calle/numero obvio, no gastes geocode todavia
    addr = (lead.get("address") or raw_hint or "").strip()
    if not re.search(r"\d{1,6}", addr) and not (lead.get("zip") and len(addr) > 10):
        return {
            "status": "need_more",
            "gaps": ["calle_y_numero"],
            "client_hint": "me pasas la calle con el numero?",
            "geo": None,
        }

    query = _compose_address_query(lead, raw_hint)
    geo = geocode_address(query)
    # reintento solo con address+zip si fallo
    if not geo and lead.get("address") and lead.get("zip"):
        geo = geocode_address(f"{lead.get('address')}, {lead.get('zip')}, USA")
    # tercer intento: solo el texto crudo del cliente
    if not geo and (raw_hint or "").strip():
        geo = geocode_address(f"{raw_hint.strip()}, USA")

    if not geo:
        upsert_lead(psid, address_verified="fail")
        return {
            "status": "fail",
            "gaps": ["direccion"],
            # corto: no spamear listas largas
            "client_hint": "no me quedo clara la direccion, me la pasas de nuevo? calle y zip",
            "geo": None,
        }

    # aplicar normalizacion
    street = (geo.get("address") or "").strip()
    city = (geo.get("city") or lead.get("city") or "").strip()
    state = (geo.get("state") or lead.get("state") or "").strip()
    zipc = (geo.get("zip") or lead.get("zip") or "").strip()[:5]
    precise = bool(geo.get("precise"))

    # state a 2 letras si viene largo
    if state and len(state) > 2:
        # si es nombre largo de census/nominatim, dejalo; Safelite prefiere 2 letras
        pass

    fields = {
        "address": street or (lead.get("address") or ""),
        "city": city,
        "state": state,
        "zip": zipc,
        "lat": geo.get("lat") or "",
        "lng": geo.get("lng") or "",
        "address_formatted": geo.get("formatted") or "",
        "address_verified": "ok" if precise and zipc else "partial",
    }
    upsert_lead(psid, **fields)

    if not precise or not zipc:
        # pedi solo lo que falte
        g = []
        if not re.search(r"\d{1,6}", street or ""):
            g.append("calle_y_numero")
        if not zipc:
            g.append("zip")
        # Siempre pedirle al cliente que aclare (optar por direccion clara)
        if "calle_y_numero" in g and "zip" in g:
            hint = "casi, me falta la calle con numero y el código postal"
        elif "calle_y_numero" in g:
            hint = "casi, me falta la calle con el numero"
        elif "zip" in g:
            hint = "casi, me falta el código postal"
        else:
            hint = "me la pasas de nuevo un poco mas clara?"
        return {
            "status": "need_more",
            "gaps": g or ["direccion"],
            "client_hint": hint,
            "geo": geo,
        }

    # chequear que el ZIP sea servible en Safelite (si falla, aun asi dejamos dir ok)
    zinfo = safelite_zip(zipc)
    if zinfo is not None and zinfo.get("isServiceable") is False:
        return {
            "status": "ok",
            "gaps": [],
            "client_hint": "",
            "geo": geo,
            "safelite_unserviceable": True,
        }

    note(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] DIR OK via {geo.get('provider')} "
        f"psid={psid} → {fields.get('address')}, {city} {state} {zipc}"
    )
    return {"status": "ok", "gaps": [], "client_hint": "", "geo": geo}



# ---------------------------------------------------------------------------
# Precios + vendedor cerrador
# ---------------------------------------------------------------------------
PRECIO_AUTO = 210   # piso: 2010 para abajo
PRECIO_GRANDE = 210  # ya no se usa por tamaño; el año manda

# Customer-specific pricing overrides belong in private configuration.
_customer_PSIDS: set[str] = set()
_customer_NAME_RE = re.compile(r"(?!)")


def _is_large_vehicle(make: str = "", model: str = "", text: str = "") -> bool:
    blob = f"{make} {model} {text}".lower()
    keys = [
        "jeepeta", "jeep", "suv", "pickup", "truck", "camioneta", "van",
        "minivan", "4x4", "suburban", "tahoe", "expedition", "sequoia",
        "armada", "yukon", "escalade", "navigator", "highlander", "pilot",
        "4runner", "pathfinder", "explorer", "traverse", "durango", "grand cherokee",
        "wrangler", "bronco", "tahoe", "silverado", "f-150", "f150", "f250",
        "f-250", "ram ", "tundra", "titan", "ranger", "colorado", "canyon",
        "odyssey", "sienna", "carnival", "transit", "sprinter", "grande",
    ]
    return any(k in blob for k in keys)


def _is_customer_lead(lead: dict | None = None, psid: str | None = None) -> bool:
    """No customer-specific price override is configured in this public copy."""
    lead = lead or {}
    pid = str(psid or lead.get("psid") or "").strip()
    if pid and pid in _customer_PSIDS:
        return True
    blob = " ".join(
        str(lead.get(k) or "") for k in ("name", "admin_note", "history")
    )
    # Inspect the lead history for configured override terms.
    if _customer_NAME_RE.search(lead.get("name") or ""):
        return True
    # Match any configured override terms in the history.
    try:
        hist = lead.get("history") or "[]"
        if isinstance(hist, str) and _customer_NAME_RE.search(hist[:2000]):
            # solo si parece el lead de tundra/hemet o ya es conocido
            if "tundra" in hist.lower() or "92545" in hist or "customer" in hist.lower() or "customer" in hist.lower():
                return True
    except Exception:
        pass
    return False


def _parse_price_int(val) -> int | None:
    if val is None or val == "":
        return None
    try:
        n = int(re.sub(r"[^\d]", "", str(val)) or "0")
        return n if 100 <= n <= 999 else None
    except Exception:
        return None


def _price_already_said_in_history(lead: dict | None) -> int | None:
    """Primer precio $2xx ya dicho al cliente (humano o bot). No se cambia después."""
    lead = lead or {}
    locked = _parse_price_int(lead.get("quoted_price"))
    if locked:
        return locked
    try:
        hist = lead.get("history")
        if isinstance(hist, str):
            hist = json.loads(hist or "[]")
        if not isinstance(hist, list):
            return None
        # orden cronológico: primer precio dicho por assistant/page
        for m in hist:
            role = (m.get("role") or "").lower()
            if role not in ("assistant", "page", "bot", "agent"):
                continue
            text = m.get("content") or m.get("text") or ""
            low = text.lower()
            # precio suelto: 210 / $210 / 210$ — NO capturar años (2015, 2017…)
            # (?!\d) evita que 2015 cuente como 201
            found = re.findall(
                r"(?:~?\s*\$\s*)?(?<!\d)(2[0-9]{2})(?!\d)(?:\s*\$|\s*usd)?",
                low,
            )
            # contexto de precio: "queda en 210", "serian 210"
            found2 = re.findall(
                r"(?:precio|coste|costo|queda en|serian|sería|seria|sale|garantia)\s*"
                r"(?:en\s*)?~?\$?\s*(?<!\d)(2[0-9]{2})(?!\d)",
                low,
            )
            for g in found + found2:
                n = int(g)
                # rangos de precio reales (no 200-209 raros salvo 200)
                if n in (200, 210, 220, 230, 240, 250, 260, 270, 280, 290, 300, 310, 320, 350) or 200 <= n <= 350:
                    # excluir si parece año embebido ya cubierto por (?!\d)
                    if n >= 200:
                        return n
    except Exception:
        log.exception("price_from_history")
    return None


def _parse_year(val) -> int | None:
    try:
        y = int(re.sub(r"[^\d]", "", str(val or ""))[:4] or "0")
    except Exception:
        return None
    if 1950 <= y <= 2030:
        return y
    return None


def precio_por_ano(year: str | int | None) -> int | None:
    """
    Precio SOLO por año (marca/modelo no importan). Un numero redondo, justo.

    - 2010 para abajo: 210
    - 2011 a 2020: 220–280 (el del medio según qué tan nuevo)
    - 2021 a 2026: 280–320
    """
    y = _parse_year(year)
    if y is None:
        return None
    if y <= 2010:
        return 210
    if y <= 2020:
        t = (y - 2011) / 9.0
        return int(round((220 + t * 60) / 10.0) * 10)
    y = min(y, 2026)
    t = (max(y, 2021) - 2021) / 5.0
    return int(round((280 + t * 40) / 10.0) * 10)


def quote_price(
    year: str | int | None = None,
    make: str = "",
    model: str = "",
    text: str = "",
    lead: dict | None = None,
    psid: str | None = None,
) -> int:
    """
    Precio de negocio por AÑO.
    - Customer-specific overrides are omitted from the public copy.
    - Si ya se cotizó: ESE precio (no cambia a mitad).
    - Si hay año: franja 210 / 220-280 / 280-320, numero redondo.
    - Sin año: 210 de piso, sin bloquear.
    """
    lead = lead or {}
    pid = psid or lead.get("psid")

    if _is_customer_lead(lead, pid):
        p = PRECIO_AUTO
        _lock_quoted_price(pid, p, lead)
        return p

    already = _price_already_said_in_history(lead)
    if already is not None:
        _lock_quoted_price(pid, already, lead)
        return already

    y = _parse_year(year) or _parse_year(lead.get("year"))
    p = precio_por_ano(y)
    if p is None:
        return PRECIO_AUTO
    _lock_quoted_price(pid, p, lead)
    return p


def _lock_quoted_price(psid: str | None, price: int, lead: dict | None = None) -> None:
    """Guarda el precio dicho. Si ya hay uno distinto, NO lo pisa (unless a private override is configured)."""
    if not psid or not price:
        return
    lead = lead or {}
    try:
        existing = _parse_price_int(lead.get("quoted_price"))
        if _is_customer_lead(lead, psid):
            if existing != PRECIO_AUTO:
                upsert_lead(psid, quoted_price=str(PRECIO_AUTO))
                lead["quoted_price"] = str(PRECIO_AUTO)
            return
        if existing is not None:
            # ya hay precio: no cambiar (evita 210→260)
            return
        upsert_lead(psid, quoted_price=str(int(price)))
        lead["quoted_price"] = str(int(price))
    except Exception:
        log.exception("lock_quoted_price")


def _is_english_text(text: str) -> bool:
    """True si el mensaje se siente principalmente en ingles."""
    t = (text or "").strip()
    if not t:
        return False
    low = t.lower()
    # español fuerte gana
    if re.search(
        r"[áéíóúñ¿¡]|"
        r"\b(hola|buenas|cu[aá]nto|cuanto|precio|parabrisas|direcci[oó]n|d[oó]nde|"
        r"gracias|porfa|necesito|quiero|carro|camioneta|jeepeta|domicilio|"
        r"usted|ustedes|ma[nñ]ana|hoy|semana|cita|agendar|ubicad)\b",
        low,
    ):
        return False
    if re.fullmatch(
        r"(ok|okay|okey|k|yes|no|si|sí|zip|cp|hola|hi)[.!? ]*",
        low,
    ):
        return False
    en_hits = len(
        re.findall(
            r"\b(hi|hello|hey|how much|price|cost|windshield|windscreen|address|"
            r"please|thanks|thank you|looking for|need|can you|where are|"
            r"located|available|appointment|schedule|replace|cracked|front|"
            r"sensor|warranty|asap|tomorrow|today|"
            r"vehicle|truck|suv|my name is|i'?m|i am|do you|"
            r"would|could|service|mobile|shop)\b",
            low,
        )
    )
    # letras latinas sin tildes + palabras EN
    if en_hits >= 2:
        return True
    if en_hits >= 1 and len(low.split()) <= 12:
        return True
    # frase casi solo ingles (how much / hi alone)
    if re.fullmatch(r"(hi|hello|hey|how much|price\??|thanks|thank you)[.!? ]*", low):
        return True
    return False


def _client_lang(lead: dict | None = None, text: str = "", psid: str | None = None) -> str:
    """
    Idioma de respuesta: 'en' o 'es'.
    Prioridad: ultimo mensaje del cliente → historial reciente de user → default es.
    """
    lead = lead or {}
    if text and _is_english_text(text):
        # Un "ok" / frase corta no voltea el chat si ya venía en español.
        if not re.fullmatch(r"(?i)[\W_]*(ok|okay|okey|k|yes|no)[\W_]*", text or ""):
            return "en"
    if text and re.search(
        r"[áéíóúñ¿¡]|\b(hola|cuanto|precio|parabrisas|direcci|gracias|necesito)\b",
        (text or "").lower(),
    ):
        return "es"
    # historial user
    samples = []
    try:
        pid = psid or lead.get("psid")
        hist = lead.get("history")
        if isinstance(hist, str):
            hist = json.loads(hist or "[]")
        if isinstance(hist, list):
            for m in hist:
                if (m.get("role") or "") == "user":
                    samples.append(m.get("content") or "")
        if pid and not samples:
            for m in history(pid)[-8:]:
                if (m.get("role") or "") == "user":
                    samples.append(m.get("content") or "")
    except Exception:
        pass
    recent = samples[-4:]
    en_n = sum(1 for s in recent if _is_english_text(s))
    es_n = sum(
        1
        for s in recent
        if re.search(
            r"[áéíóúñ¿¡]|\b(hola|cuanto|precio|parabrisas|direcci|gracias|necesito|carro)\b",
            (s or "").lower(),
        )
    )
    if en_n > es_n and en_n > 0:
        return "en"
    if en_n >= 1 and es_n == 0:
        return "en"
    return "es"


def _t(lang: str, es: str, en: str) -> str:
    return en if (lang or "es").startswith("en") else es


def price_line(year=None, make="", model="", text="", lead: dict | None = None, psid: str | None = None, lang: str | None = None) -> str:
    """Precio al cliente CON $: '210$'."""
    lead = lead or {}
    p = quote_price(year, make, model, text, lead=lead, psid=psid)
    return _fmt_price_num(p)


def price_announce(lead: dict | None = None, text: str = "", lang: str | None = None) -> str:
    """En cuanto hay modelo: una linea con el precio. Vacio si ya se lo dijimos."""
    lead = _complete_vehicle_fields(lead or {})
    if not _has_vehicle(lead):
        return ""
    if _price_already_said_in_history(lead):
        return ""
    lg = lang or _client_lang(lead, text, psid=lead.get("psid"))
    pl = price_line(
        lead.get("year"), lead.get("make") or "", lead.get("model") or "",
        text, lead=lead, psid=lead.get("psid"), lang=lg,
    )
    label = _vehicle_label(lead)
    return _t(lg, f"Ok, {label} — queda en {pl}.", f"Got it, {label} — {pl}.")


def _fmt_price_num(p: int | str | None) -> str:
    """Siempre con signo: 220$."""
    n = _parse_price_int(p) or PRECIO_AUTO
    return f"{n}$"


def _hist_texts(lead: dict | None, role: str = "assistant") -> list[str]:
    lead = lead or {}
    try:
        hist = lead.get("history")
        if isinstance(hist, str):
            hist = json.loads(hist or "[]")
        if not isinstance(hist, list):
            return []
        out = []
        for m in hist:
            if (m.get("role") or "").lower() != role:
                continue
            t = (m.get("content") or m.get("text") or "").strip()
            if t:
                out.append(t)
        return out
    except Exception:
        return []


def _times_asked(lead: dict | None, kind: str) -> int:
    """Cuantas veces el bot ya pidio zip / calle / auto / tel (para no hostigar)."""
    pats = {
        "zip": re.compile(
            r"(?i)(\bzip\b|c[oó]digo postal|postal code|tu zip|pasas (el |tu )?zip|"
            r"send (your )?zip|me pasas tu zip)",
        ),
        "address": re.compile(
            r"(?i)(direcci[oó]n|calle con n[uú]mero|calle y zip|me pasas tu dir|dame tu dir|"
            r"street and zip|send (street|your address)|"
            r"your address|what('?s| is) your address|donde te visitamos)",
        ),
        "vehicle": re.compile(
            r"(?i)(qu[eé]\s+carro|a[nñ]o,?\s*marca|marca y modelo|qu[eé]\s+auto|"
            r"qu[eé]\s+modelo|modelo de (tu )?vehic|year,? make|what('?s| is) the car|"
            r"make and model|year make model|what model)",
        ),
        "phone": re.compile(
            r"(?i)(tel[eé]fono|tu n[uú]mero|nombre y tel|name and phone|phone number)",
        ),
    }
    pat = pats.get(kind)
    if not pat:
        return 0
    return sum(1 for t in _hist_texts(lead, "assistant") if pat.search(t))


def _already_asked(lead: dict | None, kind: str, max_times: int = 1) -> bool:
    """True si ya pedimos ese dato >= max_times veces → no volver a hostigar."""
    return _times_asked(lead, kind) >= max_times


def _has_vehicle(lead: dict | None) -> bool:
    lead = _complete_vehicle_fields(lead or {})
    return bool(lead.get("year") and lead.get("make") and lead.get("model"))


def _vehicle_label(lead: dict | None) -> str:
    lead = _complete_vehicle_fields(lead or {})
    parts = [str(lead.get("make") or "").strip(), str(lead.get("model") or "").strip(), str(lead.get("year") or "").strip()]
    return " ".join(p for p in parts if p).strip()


def _has_address_ok(lead: dict | None) -> bool:
    lead = lead or {}
    return (lead.get("address_verified") or "") == "ok"


def _lead_zip(lead: dict | None) -> str:
    """ZIP de 5 digitos del lead (campo zip o extraido de address)."""
    lead = lead or {}
    z = (lead.get("zip") or "").strip()
    if re.fullmatch(r"\d{5}", z):
        return z
    z2 = _extract_zip(lead.get("address") or "") or _extract_zip(z)
    return z2 if z2 and re.fullmatch(r"\d{5}", z2) else ""


def _slots_ready(lead: dict | None) -> bool:
    """Auto + ZIP = listo para consultar horarios LIVE (NO hace falta calle)."""
    lead = lead or {}
    return bool(_has_vehicle(lead) and _lead_zip(lead))


def _street_ready(lead: dict | None) -> bool:
    """Calle real (para domicilio al cerrar). No bloquea ver horarios."""
    lead = lead or {}
    if _has_address_ok(lead):
        return True
    addr = (lead.get("address") or "").strip()
    return bool(addr and _looks_like_address(addr))


def _client_unsure_or_offtopic_model(text: str = "") -> bool:
    """Fatima Castro: 'dos puertas', 'Simi Valley', 'no sé, le pregunto a mi hijo'."""
    t = _norm_es(text or "")
    if not t:
        return False
    if re.search(
        r"\b(no\s+se|no\s+s[eé]|no\s+lo\s+s[eé]|no\s+me\s+acuerdo|"
        r"pregunto\s+a\s+mi\s+(hijo|esposo|esposa|hermano)|"
        r"le\s+voy\s+a\s+preguntar|no\s+estoy\s+seguro|"
        r"i\s+don'?t\s+know|not\s+sure|ask\s+my\s+(son|husband|wife))\b",
        t,
    ):
        return True
    if re.search(
        r"\b((\d\s*)?puertas?|sedan|coupe|hatchback|camioneta|pickup|"
        r"suv|4x4|dos\s+puertas|cuatro\s+puertas|2\s*door|4\s*door)\b",
        t,
    ):
        return True
    if re.search(
        r"\b(area|área|zona|katy|simi|valley|trabajan\s+para|"
        r"quedan\s+cerca|en\s+[a-z]{3,})\b",
        t,
    ) and not re.search(r"\b(civic|camry|accord|corolla|f[- ]?1[45]0|mustang)\b", t):
        return True
    return False


def _when_pref_hint(text: str = "") -> str:
    """Si el cliente ya dijo cuándo (finde, mañana…), lo reconocemos al pedir el ZIP."""
    t = _norm_es(text or "")
    if re.search(r"\b(fin(es)?\s+de\s+semana|el\s+finde|weekend)\b", t):
        return "fin de semana"
    if re.search(r"\bpasado\s*manana\b", t):
        return "pasado mañana"
    if re.search(r"\bmanana\b", t):
        return "mañana"
    if re.search(r"\b(hoy|oy)\b", t):
        return "hoy"
    if re.search(r"\bcualquier\s+(horario|hora|dia)\b", t):
        return "lo más pronto"
    m = re.search(r"\b(lunes|martes|miercoles|jueves|viernes|sabado|domingo)\b", t)
    if m:
        return m.group(1)
    return ""


def _asks_for_zip(text: str) -> bool:
    return bool(re.search(r"(?i)(\bzip\b|c[oó]digo postal|\bcp\b|c[oó]digo\s*zip)", text or ""))


ASK_ZIP_ES = "¿Cuál es tu código postal?"
ASK_ZIP_EN = "What is your ZIP code?"


def _ask_zip_now(lead: dict | None = None, text: str = "") -> str:
    """
    Auto listo y sin ZIP → pedir el código postal. Siempre la misma frase.
    Nunca 'te resumo el carro'.
    """
    lead = lead or {}
    lang = _client_lang(lead, text, psid=lead.get("psid"))
    when = _when_pref_hint(text)
    ask = _t(lang, ASK_ZIP_ES, ASK_ZIP_EN)
    if when:
        return _t(lang, f"Ok, {when}. {ASK_ZIP_ES}", f"Got it, {when}. {ASK_ZIP_EN}")
    return ask


def _next_missing_ask(
    lead: dict | None = None,
    force_blocker: bool = False,
    text: str = "",
    lang: str | None = None,
) -> str:
    """
    UNA pregunta: auto → ZIP → horarios.
    NUNCA pedir calle/nombre/tel (cierre manual del humano).
    """
    lead = lead or {}
    lang = lang or _client_lang(lead, text, psid=lead.get("psid"))
    y, mk, md = lead.get("year"), (lead.get("make") or "").strip(), (lead.get("model") or "").strip()
    if not (y and mk and md):
        # Si ya hay parte del auto, pedir EL DATO QUE FALTA no es hostigar:
        # es avanzar. Antes el guard de "ya pregunte" se comia estas ramas y
        # a un cliente que ya habia dicho "toyota tundra" se le contestaba
        # "cuando me digas el carro..." — quedaba trabado ahi para siempre.
        if _already_asked(lead, "vehicle") and not force_blocker and not (mk or md or y):
            return _fb_call_line(lead, text)
        if y and mk and not md:
            if _client_unsure_or_offtopic_model(text) or _times_asked(lead, "vehicle") >= 2:
                return _ask_zip_now(lead, text)
            return _t(
                lang,
                f"Gracias, {mk} {y}. ¿Qué modelo es?",
                f"Thanks, {y} {mk}. What model is it?",
            )
        if mk and md and not y:
            return _t(lang, f"Gracias. ¿De qué año es tu {mk} {md}?", f"Thanks. What year is your {mk} {md}?")
        if y and not mk:
            return _t(lang, "Gracias. ¿Qué marca es?", "Thanks. What is the make?")
        if mk and not md and not y:
            return _t(lang, f"Gracias. ¿De qué año es tu {mk}?", f"Thanks. What year is your {mk}?")
        return _t(
            lang,
            "¡Claro, te ayudo por aquí! ¿Qué marca, modelo y año es tu vehículo?",
            "Of course, I can help you here! What make, model, and year is your vehicle?",
        )
    if not _lead_zip(lead):
        if _already_asked(lead, "zip"):
            return _fb_call_line(lead, text)
        return _ask_zip_now(lead, text)
    if not lead.get("slot_id"):
        return _t(lang, "¿Qué día te acomoda de esos?", "Which of those days works for you?")
    return _t(
        lang,
        "Anoté tu preferencia. Una persona del equipo te confirma por este chat.",
        "Got it — day noted. Someone will message you shortly to confirm.",
    )


def _blocker_need(lead: dict | None, need: str, text: str = "") -> str:
    """Solo cuando de verdad no se puede continuar sin ese dato."""
    lang = _client_lang(lead, text, psid=(lead or {}).get("psid"))
    if need in ("zip", "address", "street"):
        # NUNCA pedir calle — solo ZIP para horarios
        return _ask_zip_now(lead, text)
    if need == "vehicle":
        # Si ya hay Ford 2016, preguntar SOLO el modelo. Nunca resetear a
        # "año marca modelo" (Mauricio Limon: el bot ya tenia 2 de 3).
        return _next_missing_ask(lead, force_blocker=True, text=text)
    if need == "phone":
        return _fb_call_line(lead, text)
    return _t(lang, "Necesito ese dato para continuar.", "I need that to continue.")


def _reply_ask_vehicle_short(lead: dict | None = None, text: str = "") -> str:
    lead = lead or {}
    return _next_missing_ask(lead, text=text)


def _rule_fallback_reply(lead: dict | None, text: str = "", analysis: dict | None = None) -> str:
    """
    Si el LLM falla (429, etc.): responder con reglas de negocio.
    Flujo: auto → precio YA + zip → horarios (otro paso) → humano.
    """
    lead = _complete_vehicle_fields(lead or {})
    analysis = analysis or {}
    text = text or ""
    lang = _client_lang(lead, text, psid=lead.get("psid"))

    if analysis.get("is_opt_out") or _client_wants_out(text):
        return _t(lang, "Listo, sin problema. Cuando quieras me escribes.",
                  "All good. Message me whenever you want.")

    if analysis.get("is_price") or analysis.get("primary") == "price":
        if not _lead_zip(lead):
            return _reply_vehicle_short(lead, text) if _has_vehicle(lead) else _next_missing_ask(lead, text=text)
        return _reply_price_short(lead, text)

    if analysis.get("is_vehicle") and _has_vehicle(lead):
        return _reply_vehicle_short(lead, text)

    if _has_vehicle(lead) and not _lead_zip(lead):
        return _reply_vehicle_short(lead, text)

    if analysis.get("is_address") or re.fullmatch(r"\s*\d{5}\s*", text):
        if _has_vehicle(lead) and _lead_zip(lead):
            return _t(lang, "Dame un segundo, te busco horarios.", "One sec, pulling times.")
        if not _has_vehicle(lead):
            return _next_missing_ask(lead, text=text)

    return _next_missing_ask(lead, text=text)


def wants_price(text: str, analysis: dict | None = None) -> bool:
    """True si el cliente está pidiendo el costo (costo, presio, cuanto...)."""
    if analysis and (analysis.get("is_price") or analysis.get("primary") == "price"):
        return True
    low = _norm_es(text or "")
    return bool(
        re.search(
            r"\b(precio|presio|predio|costo|coste|vale|cotiz|how much|price|"
            r"cuanto|cuánto|cuanto sale|cuanto cuesta|kero\s+presio)\b",
            low,
        )
    ) or bool(re.search(r"\$\s*\d{2,4}", text or ""))


def _price_only_line(lead: dict | None = None, text: str = "") -> str:
    """Solo el monto. Sin pedir día, zip ni carro (Sergio: pidió costo y había que dárselo)."""
    lead = lead or {}
    y = (lead.get("year") or "").strip()
    mk = (lead.get("make") or "").strip()
    md = (lead.get("model") or "").strip()
    pl = price_line(y, mk, md, text, lead=lead, psid=lead.get("psid"), lang="es")
    if y and mk and md:
        return f"Para tu {mk} {md} {y} queda en {pl}."
    return f"El cambio queda en {pl}."


def _reply_price_short(lead: dict | None = None, text: str = "") -> str:
    """Precio en una línea. Número + zip si falta. Sin recap del carro."""
    lead = lead or {}
    lang = _client_lang(lead, text, psid=lead.get("psid"))
    low = _norm_es(text or "")
    y = lead.get("year")
    zip_corto = _t(
        lang,
        "Envíeme su código postal para revisar disponibilidad.",
        "Send me your zip code to check availability.",
    )
    if not y:
        return _t(
            lang,
            "Desde 210$ con instalación incluida. ¿Qué marca, modelo y año es tu vehículo?",
            "From 210$ with installation included. What make, model, and year is your vehicle?",
        )
    n = quote_price(y, lead.get("make") or "", lead.get("model") or "", text, lead=lead, psid=lead.get("psid"))
    money = _fmt_price_num(n)
    honor = bool(re.search(r"\b(honor|honran|anuncio|publicaci[oó]n)\b", low))
    nada = bool(
        re.search(r"\b(nada mas|nada más|solo eso|fijo)\b", low)
        or (re.search(r"\$\s*\d{2,4}", text or "") and re.search(r"\?", text or ""))
    )
    if honor or nada:
        base = _t(lang, f"Sí, {money} con instalación incluida.", f"Yes, {money} with installation included.")
    else:
        base = _t(lang, f"{money} con instalación incluida.", f"{money} with installation included.")
    if _already_showed_slots(lead):
        return base
    if not _lead_zip(lead):
        return f"{base} {zip_corto}"
    return base


def _reply_vehicle_short(lead: dict, text: str = "") -> str:
    """Auto listo → precio YA y zip. Nada más."""
    lead = _complete_vehicle_fields(lead)
    lang = _client_lang(lead, text, psid=lead.get("psid"))
    y, mk, md = lead.get("year"), lead.get("make") or "", lead.get("model") or ""
    if not (y and mk and md):
        return _next_missing_ask(lead, text=text)
    money = _fmt_price_num(
        quote_price(y, mk, md, text, lead=lead, psid=lead.get("psid"))
    )
    if _lead_zip(lead):
        return _t(lang, f"{money} con instalación incluida.", f"{money} with installation included.")
    zip_corto = _t(
        lang,
        "Envíeme su código postal para revisar disponibilidad.",
        "Send me your zip code to check availability.",
    )
    return _t(
        lang,
        f"{money} con instalación incluida. {zip_corto}",
        f"{money} with installation included. {zip_corto}",
    )


def _reply_greeting_short(lead: dict | None = None, text: str = "") -> str:
    lead = lead or {}
    lang = _client_lang(lead, text, psid=lead.get("psid"))
    if _has_vehicle(lead) and not _lead_zip(lead):
        return _t(lang, f"Hola. {ASK_ZIP_ES}", f"Hi. {ASK_ZIP_EN}")
    if _has_vehicle(lead):
        return _t(
            lang,
            f"Hola — seguimos con tu {_vehicle_label(lead)}.",
            f"Hi — still on your {_vehicle_label(lead)}.",
        )
    if _already_asked(lead, "vehicle"):
        return _fb_call_line(lead, text)
    return _t(
        lang,
        "¡Claro, te ayudo por aquí! ¿Qué marca, modelo y año es tu vehículo?",
        "Of course, I can help you here! What make, model, and year is your vehicle?",
    )


def _reply_location_short(lead: dict | None = None, text: str = "") -> str:
    """¿Dónde están? — a domicilio; prioriza ZIP para horarios."""
    lead = lead or {}
    lang = _client_lang(lead, text, psid=lead.get("psid"))
    if _has_vehicle(lead):
        if _lead_zip(lead):
            return _t(
                lang,
                f"Servicio en los 50 estados. {ASK_ZIP_ES}",
                f"Service in all 50 states. {ASK_ZIP_EN}",
            )
        return _t(
            lang,
            f"Servicio en los 50 estados. {_ask_zip_now(lead, text)}",
            f"Service in all 50 states. {_ask_zip_now(lead, text)}",
        )
    next_question = _next_missing_ask(
        lead,
        force_blocker=True,
        text=text,
        lang=lang,
    )
    return _t(
        lang,
        f"Servicio en los 50 estados. {next_question}",
        f"Service in all 50 states. {next_question}",
    )


def _strip_reask_known(reply: str, lead: dict | None = None) -> str:
    """Si ya sabemos auto/dir/tel, quita o reescribe frases que lo vuelven a pedir."""
    lead = lead or {}
    t = (reply or "").strip()
    if not t:
        return t

    if _has_vehicle(lead):
        # bloquea "que carro / año marca modelo / que auto tienes"
        if re.search(
            r"(?i)(qu[eé]\s+carro|qu[eé]\s+auto|a[nñ]o,?\s*marca\s*y\s*modelo|"
            r"marca\s*y\s*modelo|modelo\s+de\s+(vehiculo|vehículo|carro|auto)|"
            r"dime el carro|pasame (el )?auto|qu[eé]\s+veh[ií]culo)",
            t,
        ):
            # si SOLO pedía el auto → reemplazar por siguiente missing
            if re.search(
                r"(?i)^(claro|ok|perfecto|dale|hola)?[,.]?\s*"
                r"(qu[eé]\s+carro|qu[eé]\s+auto|a[nñ]o)",
                t,
            ) or len(t) < 120:
                t = _next_missing_ask(lead)
            else:
                t = re.sub(
                    r"(?i)[^.?!]*(qu[eé]\s+carro|qu[eé]\s+auto|a[nñ]o,?\s*marca\s*y\s*modelo|"
                    r"marca\s*y\s*modelo)[^.?!]*[.?!]?\s*",
                    "",
                    t,
                ).strip()
                if not t or len(t) < 8:
                    t = _next_missing_ask(lead)

    if _lead_zip(lead) and re.search(r"(?i)(pasas tu zip|dame tu zip|me pasas (el |tu )?zip|c[oó]digo postal)", t):
        t = re.sub(
            r"(?i)[^.?!]*(pasas tu zip|dame tu zip|me pasas (el |tu )?zip)[^.?!]*[.?!]?\s*",
            "",
            t,
        ).strip()
        if not t:
            t = "¿Qué día te acomoda?" if not lead.get("slot_id") else "¿En qué te ayudo?"
    if _street_ready(lead) or _has_address_ok(lead):
        if re.search(r"(?i)(pasas tu direcci[oó]n|dame tu direcci[oó]n|direcci[oó]n completa|calle con n[uú]mero|calle.*zip)", t):
            t = re.sub(
                r"(?i)[^.?!]*(pasas tu direcci[oó]n|dame tu direcci[oó]n|direcci[oó]n completa|calle con n[uú]mero|calle y zip)[^.?!]*[.?!]?\s*",
                "",
                t,
            ).strip()
            if not t:
                t = "¿Qué día te acomoda?" if not lead.get("slot_id") else "¿En qué te ayudo?"

    if lead.get("phone") and re.search(r"(?i)(n[uú]mero de tel[eé]fono|tu tel[eé]fono|pasas.*tel)", t):
        t = re.sub(
            r"(?i)[^.?!]*(n[uú]mero de tel[eé]fono|tu tel[eé]fono|pasas.*tel[eé]fono)[^.?!]*[.?!]?\s*",
            "",
            t,
        ).strip() or t

    return t.strip()


def _trim_habladero(reply: str) -> str:
    """Corta relleno: 1-2 frases max, compacto siempre."""
    t = (reply or "").strip()
    if not t:
        return t
    # frases tipicas de relleno → fuera
    t = re.sub(
        r"(?i)\s*(con toda (la )?comodidad|sin compromiso|estoy para ayudarte|"
        r"quedo a la orden|no dudes en|te invito a|es una excelente opci[oó]n|"
        r"aprovecha|no te quedes sin|fisura crece|conviene hoy|"
        r"agendar una cita a domicilio|proporcioname|proporcionar|"
        r"quedo atento|cualquier consulta|no dudes|con gusto|"
        r"para poder ayudarte|en que puedo ayudarte)\s*[.,!]?\s*",
        " ",
        t,
    )
    t = re.sub(r"[ \t]{2,}", " ", t).strip()
    t = re.sub(r"\n{3,}", "\n\n", t)
    # max 2 oraciones (salvo listas de horarios con bullets)
    if "•" not in t and "\n" not in t:
        parts = re.split(r"(?<=[.!?…])\s+", t)
        parts = [p for p in parts if p.strip()]
        if len(parts) > 2:
            t = " ".join(parts[:2]).strip()
    # tope duro
    if len(t) > 280 and "•" not in t:
        cut = t[:280]
        sp = cut.rsplit(" ", 1)[0]
        t = (sp or cut).rstrip(",;:") + ("." if not re.search(r"[.!?…]$", sp or cut) else "")
    return t.strip()


# ---------------------------------------------------------------------------
# LLM agent — respuestas cortas, solo lo que el cliente pide
# ---------------------------------------------------------------------------
def _client_style_hint(psid: str, latest_text: str = "") -> str:
    """Pistas de como escribe el cliente para espejar acento/registro."""
    samples: list[str] = []
    try:
        for m in history(psid)[-10:]:
            if (m.get("role") or "") == "user" and (m.get("content") or "").strip():
                samples.append((m.get("content") or "").strip())
    except Exception:
        pass
    if (latest_text or "").strip():
        samples.append(latest_text.strip())
    blob = "\n".join(samples[-6:])
    if not blob.strip():
        return "sin historial: habla simple, claro, como chat de Messenger USA/Latam"

    low = blob.lower()
    flags: list[str] = []
    # formalidad
    if re.search(r"\b(usted|ustedes|podr[ií]a|quisiera|buenos d[ií]as|buenas tardes|disculpe)\b", low):
        flags.append("trato de usted / un poco formal")
    if re.search(r"\b(vos|pasame|mandame|decime|contame|dale|che)\b", low):
        flags.append("voseo / rioplatense o informal 'vos'")
    if re.search(r"\b(tu|t[uú]|pasame|dame|ok|okie|hola)\b", low) and "usted" not in low:
        flags.append("tuteo informal")
    # ingles / spanglish
    if _is_english_text(blob) or re.search(
        r"\b(how much|price|address|zip|windshield|can you|please|hello|hi|looking for)\b", low
    ):
        flags.append(
            "CLIENTE EN INGLES → responde TODO en ingles natural (no español). "
            "price like '210$'"
        )
    # caribe / dominicano / PR / centro
    if re.search(r"\b(jeepeta|parabrisas|cristal|frente|kiero|ke|q |xk|xq|porfa|plis|wawawa|tiguere|vaina|pana|bro|mano)\b", low):
        flags.append("jerga caribe/centro o chat rapido")
    # mexico
    if re.search(r"\b(we|güey|guey|mames|órale|orale|porfa|plis|camioneta|parabrisas)\b", low):
        flags.append("toque mexicano/informal")
    # escritura floja
    if re.search(r"\b(q|k|xq|xk|tb|tmb|pos|ps|ta|tmbn|dnde|dnd|kiero|nesecito|nesecito|direcion|direksion)\b", low) or re.search(r"(.)\1{2,}", low):
        flags.append("escribe con faltas/abreviaturas → NO corrijas ni hables de libro")
    if not re.search(r"[áéíóúñ¿¡]", blob) and re.search(r"[aeiou]", low):
        flags.append("casi sin tildes → vos tambien podes ir sin tildes")
    if len(blob) < 40 or max((len(s) for s in samples), default=0) < 25:
        flags.append("mensajes cortos → responde corto")

    sample_line = " | ".join(s.replace("\n", " ")[:80] for s in samples[-3:])
    return (
        f"muestras del cliente: {sample_line}. "
        f"senales: {', '.join(flags) if flags else 'neutro informal'}. "
        "ESPEJA su forma de hablar: mismas palabras faciles que el usa "
        "(precio/cuanto, carro/jeepeta, casa/domicilio, calle/zip). "
        "Cero español de oficina."
    )


def _system_prompt() -> str:
    negocio = get_setting("negocio", NEGOCIO) if DB.exists() else NEGOCIO
    precio = get_setting("precio", PRECIO) if DB.exists() else PRECIO
    return f"""Eres el asistente de {negocio} por Messenger.
Hablas como persona real: natural, calido y CORTO (1-2 frases max). Chat de Messenger de verdad.

MISION PRINCIPAL (estructura fija)
1) AUTO (año marca modelo) → 2) ZIP → 3) HORARIOS LIVE reales. Nada mas.
- NO pidas calle, direccion, nombre ni telefono. Si piden contacto: "Puedes llamarnos por Facebook, por aquí mismo."
- NO inventes citas ni digas "ya quedo agendada" en sistema.
- Precio SOLO si preguntan cuanto cuesta. NUNCA lo sueltes solo. NUNCA digas "el anuncio", "precio del anuncio" ni "como dice el anuncio".
- Techo / trasero / laterales = NO cotices {precio}; di que un agente lo ve.

URGENCIA / TALLER (aprendizaje Angel Oliva)
- Si pide HOY y no hay envio a domicilio hoy → dilo claro y ofrece TALLER lo mas pronto (si el API tiene).
- "lo llevo / yo lo yebo / si yo lo llevo" = TALLER aunque antes haya dicho domicilio.
- Si ya se ofrecio taller y dice "ok manana" / "para manana a que hora" → es TALLER, NO vuelvas a listar solo domicilio.
- No spamees el mismo listado de sabado domicilio cuando piden hoy/manana.

FAQ / NO REPETIR HORARIOS (aprendizaje Ramon Sanchez)
- Si YA mandaste horarios, PROHIBIDO volver a pegar la lista entera de "Envio a domicilio".
- Preguntas de PRECIO → responde el numero 1 frase + "que dia te acomoda de los que te pase?"
- PAGO / al tecnico → "pago a admin (Zelle/Apple Pay/Cash App), no al tecnico." Sin lista.
- "Yo te aviso / cuando este listo" → "Perfecto, cuando estes listo me escribes." Sin lista.
- Solo re-lista horarios si pide dia/hora o "mandame horarios otra vez".

IDIOMA (OBLIGATORIO)
- Si el cliente escribe en INGLES → responde TODO en ingles.
- Si escribe en ESPAÑOL → responde en español.
- Mira "idioma_cliente" / "estilo_cliente" del CONTEXTO. No mezcles si el cliente va en un solo idioma.
- Si preguntan precio: EN 220$; ES 220$. Siempre el signo $. Sin "anuncio" ni garantia de relleno.

ESTILO
- Como chat de Messenger de verdad: "Claro"/"Sure", "Ok", "Perfecto"/"Perfect", "sin problema"/"no problem".
- ESPEJA como escribe el cliente.
- NUNCA: "proporcionarme", "agendar una cita", "cotizacion formal", "estimado",
  "con toda comodidad", "fisura crece", "aprovecha", menus 1/2/3.
- NUNCA digas Safelite, bot, IA, links ni codigos de orden. NUNCA pidas tarjeta.

PRECIOS
- Si pregunta precio/cuanto/costo → una frase con el numero y $ (220$). Si NO pregunta, NO hables de precio.
- NUNCA "como el anuncio", "precio del anuncio", "210 del ad", "con garantia" de relleno.
- Un solo numero. NUNCA lo cambies a mitad.

VENTA COMO HUMANO (no hostigar)
- Si ya pediste un dato y el cliente NO lo dio → NO lo vuelvas a pedir en cada mensaje.
- Solo si de verdad no se puede seguir (quiere dia/horario sin ZIP) di:
  "Envíeme su código postal para revisar los agentes disponibles en su zona."
- NUNCA suenes a formulario. "¿Donde estan?" → "Tenemos agentes en los 50 estados de USA y trabajamos con cita" + pide código postal/carro. NO inventes sucursal.

FLUJO (OBLIGATORIO)
1) Hola → carro si falta (año marca modelo).
2) Auto listo → pide SOLO el código postal: "Envíeme su código postal para revisar los agentes disponibles en su zona."
3) ZIP listo → el sistema muestra horarios LIVE. Tu no inventes dias/horas.
4) Si elige dia → "Anoté tu preferencia. Una persona del equipo te confirma."
5) PROHIBIDO pedir calle, direccion, nombre o telefono. NUNCA. Contacto = llamarnos por Facebook.
6) "olvidalo" → "Listo, sin problema." sin insistir.

REGLAS DE ORO — NO REPETIR
- Si ya hay year/make/model → PROHIBIDO pedir el carro de nuevo.
- Si ya hay zip → PROHIBIDO pedir zip de nuevo.
- PROHIBIDO pedir calle/direccion/nombre/tel en cualquier momento.
- 1-2 frases. Domicilio default. Nunca Safelite/links/menus.

SALIDA SOLO JSON:
{{"reply":"...","stage":"nuevo|calificando|cerrando|ganado|perdido","year":"","make":"","model":"","zip":"","address":"","city":"","state":"","phone":"","name":"","want_date":"","glass":"Windshield","vehicle_size":"auto|grande|","service_mode":"domicilio|sucursal|"}}
Vacios si no lo dijo. address vacio si no es direccion real. zip = 5 digitos si lo dijo.
"""


def _llm(messages: list, max_tokens: int = 480) -> tuple[str, str]:
    """Llama al LLM. Devuelve (content, finish_reason).

    finish_reason == 'length' = se corto por tokens (no usar el reply tal cual).
    """
    base = os.getenv("OPENAI_BASE_URL")
    key = os.getenv("OPENAI_API_KEY")
    if not (base and key):
        raise RuntimeError("LLM no configurado")
    if llm_status.is_disabled():
        raise RuntimeError(f"LLM deshabilitado: {llm_status.disabled_reason()}")
    r = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": os.getenv("MODEL_NAME", "llama-3.3-70b-versatile"),
            "temperature": 0.3,
            "max_tokens": max_tokens,
            "messages": messages,
        },
        timeout=45,
        trust_env=False,
    )
    if r.status_code == 401:
        llm_status.disable("OPENAI_API_KEY invalida")
    r.raise_for_status()
    choice = r.json()["choices"][0]
    content = (choice.get("message") or {}).get("content") or ""
    finish = (choice.get("finish_reason") or "").strip() or "stop"
    return content.strip(), finish


def _parse(raw: str) -> dict:
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        # Nunca cortar a 180: eso generaba mensajes a medias al cliente
        text = raw.strip()
        if len(text) > 900:
            text = text[:900].rsplit(" ", 1)[0].strip()
        return {"reply": text or "Un momento.", "stage": "calificando"}
    try:
        return json.loads(m.group(0))
    except Exception:
        # field scrape (JSON incompleto por corte de tokens)
        def f(k):
            # valor entre comillas, o valor sin cerrar (truncado)
            mm = re.search(rf'"{k}"\s*:\s*"((?:\\.|[^"\\])*)(?:"|$)', raw)
            if mm:
                return mm.group(1)
            mm2 = re.search(rf'"{k}"\s*:\s*"([^"]*)$', raw)
            return mm2.group(1) if mm2 else ""

        return {
            "reply": f("reply") or "Un momento.",
            "stage": f("stage") or "calificando",
            "year": f("year"),
            "make": f("make"),
            "model": f("model"),
            "zip": f("zip"),
            "address": f("address"),
            "city": f("city"),
            "state": f("state"),
            "phone": f("phone"),
            "name": f("name"),
            "want_date": f("want_date"),
            "glass": f("glass") or "Windshield",
            "vehicle_size": f("vehicle_size"),
            "service_mode": f("service_mode"),
            "_truncated_json": True,
        }


def _extract_zip(text: str) -> str:
    """Extraccion robusta (ver app/zip_norm.py): tolera puntuacion, ZIP+4 y
    digitos separados; rechaza años, telefonos y montos."""
    return zip_extract(text)


def _vale_la_pena_llm(text: str) -> bool:
    """
    Filtro barato antes de gastar una llamada al LLM.

    Solo tiene sentido preguntarle si el mensaje trae algo que PODRIA ser un
    dato (un numero, o palabras) y no es un "ok"/"gracias" suelto.
    """
    t = (text or "").strip()
    if len(t) < 4 or len(t) > 300:
        return False
    if re.fullmatch(r"(?i)[\W_]*(ok|okay|si|sí|no|gracias|thanks|bien|listo|dale|"
                    r"vale|perfecto|hola|buenas)[\W_]*", t):
        return False
    return bool(re.search(r"[a-zA-ZáéíóúñÁÉÍÓÚÑ]{3,}|\d", t))


def _capture_vehicle(psid: str, lead: dict, analysis: dict, text: str) -> dict:
    """
    Completa el auto cuando el regex no pudo.

    La gente escribe "tengo una toyota tundra del 15", "mi carro es un civic 08"
    o "es para una hinday accent 2020". Lo que saque el LLM se valida contra el
    catalogo real de Safelite antes de guardarlo.
    """
    if not psid or _has_vehicle(lead):
        return lead
    if not _vale_la_pena_llm(text):
        return lead

    try:
        veh = llm_extract_vehicle(text)
    except Exception:
        log.exception("_capture_vehicle llm")
        return lead
    if not veh:
        return lead

    campos = {k: veh[k] for k in ("year", "make", "model")
              if veh.get(k) and not (lead.get(k) or "").strip()}
    if not campos:
        return lead

    log.info(
        "auto inferido psid=%s %r -> %s (verificado=%s)",
        psid, (text or "")[:60], campos, veh.get("_verified"),
    )
    upsert_lead(psid, **campos)
    return get_lead(psid) or lead


def _capture_zip(psid: str, lead: dict, analysis: dict, text: str) -> dict:
    """
    Graba el zip apenas aparece, venga en el mensaje que venga.

    Antes el zip solo se guardaba dentro de la rama 'is_address', asi que
    "2015 Toyota Tundra 29349" o "29349." se perdian y el lead moria en
    'sin_zip' sin ver horarios. Si no hay zip explicito pero el cliente dio
    ciudad+estado, lo resuelve por geocoding.

    Devuelve el lead (actualizado si hubo cambio).
    """
    if not psid:
        return lead

    z = (analysis or {}).get("zip") or _extract_zip(text or "")
    source = "text"

    if not z and not _lead_zip(lead):
        try:
            res = resolve_zip(text, validator=safelite_zip)
        except Exception:
            log.exception("_capture_zip resolve")
            res = {}
        z, source = res.get("zip") or "", res.get("source") or ""

        # ultimo recurso: el LLM. La gente escribe "estan en Albuquerque?" o
        # "me queda cerca de charlotte" y ahi el regex no puede.
        if not z and _vale_la_pena_llm(text):
            try:
                res = llm_extract_location(text, validator=safelite_zip)
            except Exception:
                log.exception("_capture_zip llm")
                res = {}
            z, source = res.get("zip") or "", res.get("source") or ""
            if res.get("city") and not res.get("state"):
                # ciudad sin estado: no adivinamos, pero lo dejamos anotado
                upsert_lead(psid, city=res["city"])
        if z:
            log.info("zip inferido psid=%s %r -> %s (%s)", psid, (text or "")[:60], z, source)

    if not z:
        return lead

    prev = _lead_zip(lead)
    if z == prev:
        return lead

    log.info("zip capturado psid=%s %s (source=%s) prev=%s", psid, z, source, prev or "-")
    # zip nuevo → los horarios cacheados y el slot elegido ya no valen
    upsert_lead(
        psid,
        zip=z,
        slots_json="",
        slots_fetched_at="",
        slot_id="",
        slot_date="",
        slot_time="",
        slots_error="",
    )
    return get_lead(psid) or lead


def _extract_phone(text: str) -> str:
    digits = re.sub(r"\D", "", text or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


# marcas / palabras de auto (para NO guardar basura como "direccion")
_VEHICLE_MARKERS = re.compile(
    r"\b(honda|toyota|nissan|ford|chevy|chevrolet|dodge|ram|gmc|jeep|kia|hyundai|"
    r"mazda|subaru|vw|volkswagen|bmw|mercedes|audi|lexus|acura|infiniti|buick|"
    r"cadillac|chrysler|lincoln|mitsubishi|volvo|tesla|accord|acord|civic|camry|"
    r"corolla|f-?150|silverado|altima|aldima|oldina|sentra|versa|cr-?v|rav4|tahoe|suburban|"
    r"piloto|camry|carry|"
    r"carro|coche|auto|camioneta|jeepeta|truck|suv|sedan|pickup|parabrisas|"
    r"cristal|windshield|v6|v8|4cil|cilindros?)\b",
    re.I,
)
_STREET_MARKERS = re.compile(
    r"\b(st|street|ave|avenue|rd|road|ln|lane|dr|drive|ct|court|blvd|boulevard|"
    r"way|cir|circle|hwy|highway|trail|ter|terrace|pkwy|parkway|plaza|pl|"
    r"calle|avenida|camino|carretera|apto|apt|suite|ste|unit|#)\b",
    re.I,
)


def _looks_like_address(text: str) -> bool:
    """True solo si el texto parece una direccion USA real (no auto, no chat)."""
    t = (text or "").strip()
    if not t or len(t) < 5:
        return False
    low = t.lower()
    # rechazo claro: solo hablando del vehiculo / chat sin calle
    if _VEHICLE_MARKERS.search(low) and not _STREET_MARKERS.search(low) and not re.search(
        r"\b\d{1,6}\s+[A-Za-zÁÉÍÓÚáéíóúñÑ]{2,}", t
    ):
        return False
    # frases de chat que no son dir
    if re.search(
        r"\b(yo no|no te|olvid|precio|cuanto|gracias|hola|buenos|buenas|"
        r"es para|necesito|quiero|me pasas|dame)\b",
        low,
    ) and not _STREET_MARKERS.search(low) and not re.search(r"\b\d{5}\b", t):
        return False
    # patrones fuertes de direccion
    if _STREET_MARKERS.search(t) and re.search(r"\d{1,6}", t):
        return True
    # "123 Main St" / "7219 Heirloom Ln" / "8202 N 116TH"
    if re.search(r"\b\d{1,6}\s+[A-Za-zÁÉÍÓÚáéíóúñÑ#\.]{2,}", t) and (
        _STREET_MARKERS.search(t) or re.search(r"\b\d{5}\b", t) or "," in t
    ):
        return True
    # linea con zip + algo de calle
    if re.search(r"\b\d{5}\b", t) and re.search(r"[A-Za-zÁÉÍÓÚáéíóúñÑ]{3,}", t) and re.search(
        r"\d{1,6}", t
    ):
        # no confiar en "honda 2008 12345" (raro); exige no ser solo vehiculo
        if _VEHICLE_MARKERS.search(low) and not _STREET_MARKERS.search(low):
            return False
        return True
    return False


def _looks_like_vehicle_blob(text: str) -> bool:
    """Mensaje que describe el auto, no una direccion."""
    t = (text or "").strip()
    if not t:
        return False
    has_year = bool(re.search(r"\b(19|20)\d{2}\b", t))
    has_veh = bool(_VEHICLE_MARKERS.search(t))
    return (has_year or has_veh) and not _looks_like_address(t)


def _client_later(text: str) -> bool:
    """No ahora: te aviso, cuando tenga el carro/dinero, 2-3 semanas."""
    t = _norm_es(text or "")
    return bool(
        re.search(
            r"\b(yo\s+le\s+aviso|te\s+aviso|cuando\s+est[eé]\s+listo|"
            r"cuando\s+tenga(\s+el\s+dinero)?|no\s+estoy\s+listo|"
            r"entre\s+\d+\s*a\s*\d+\s*semanas|\d+\s*a\s*\d+\s*semanas|"
            r"buscando\s+precios\s+por\s+ahora|cualquier\s+cosa\s+le\s+dejo|"
            r"luego\s+te|mas\s+tarde|ahorita\s+no|"
            r"let\s+you\s+know|i'?ll\s+let)\b",
            t,
        )
    )


def _client_wants_out(text: str) -> bool:
    """Cliente se baja / cancela interes. Responder corto y no insistir."""
    t = _norm_es(text or "")
    if not t:
        return False
    return bool(
        re.search(
            r"\b(olvidalo|olvidate|dejalo|dejalo asi|ya no|"
            r"no gracias|no grasias|no me interesa|no quiero( nada| el servicio| el trabajo)?|"
            r"ya no quiero|ya consegui|cotiz[eé] por otro lado|"
            r"no sigas|para ya|basta|cancel(a|ar)? todo|"
            r"no hace falta|no necesito|despues te escribo|"
            r"mejor no|dejame en paz)\b",
            t,
        )
        or re.search(
            r"\bya (lo |me lo |me le |se lo )?(cambi\w*|instal\w*|pusieron)\b",
            t,
        )
        or re.fullmatch(r"(nop|nel|nah|paso|olvidalo|dejalo)[.!]*", t)
    )


def _client_denies_address(text: str) -> bool:
    """Cliente aclara que NO dio / no va a dar direccion."""
    t = _norm_es(text or "")
    return bool(
        re.search(
            r"\b(no te (he )?pasado|no te di|no di|no te doy|no te la doy|"
            r"no paso|no voy a (dar|pasar)|sin direccion|no doy (mi )?direccion|"
            r"ninguna direccion|no te di ninguna|no te he dado|"
            r"no quiero (dar|pasar) (la |mi )?direccion|"
            r"no te voy a dar)\b",
            t,
        )
        or re.search(r"\bdireccion\b.*\bno\b|\bno\b.*\bdireccion\b", t)
        and re.search(r"\b(pasado|di|doy|dado|pasar|dar)\b", t)
    )


def _clear_garbage_address(psid: str, lead: dict | None = None) -> dict:
    """Borra address basura (ej. texto del auto guardado como dir)."""
    lead = lead or get_lead(psid)
    addr = (lead.get("address") or "").strip()
    if not addr or _looks_like_address(addr):
        return lead
    fields = {
        "address": "",
        "address_verified": "",
        "address_formatted": "",
        "lat": "",
        "lng": "",
    }
    city = (lead.get("city") or "").strip()
    if city and (_looks_like_vehicle_blob(city) or _VEHICLE_MARKERS.search(city)):
        fields["city"] = ""
    upsert_lead(psid, **fields)
    log.info("clear garbage address psid=%s was=%r", psid, addr[:80])
    return get_lead(psid)


# Marcas conocidas (clave normalizada → nombre canónico)
_MAKE_CANON = {
    "honda": "Honda", "toyota": "Toyota", "nissan": "Nissan", "nission": "Nissan",
    "ford": "Ford",
    "chevy": "Chevrolet", "chevrolet": "Chevrolet", "dodge": "Dodge", "ram": "Ram",
    "gmc": "GMC", "jeep": "Jeep", "kia": "Kia", "hyundai": "Hyundai",
    "mazda": "Mazda", "subaru": "Subaru", "vw": "Volkswagen", "volkswagen": "Volkswagen",
    "bmw": "BMW", "mercedes benz": "Mercedes Benz", "mercedes-benz": "Mercedes Benz",
    "mercedes": "Mercedes Benz", "mercedez": "Mercedes Benz",
    "audi": "Audi", "lexus": "Lexus",
    "acura": "Acura", "infiniti": "Infiniti", "buick": "Buick", "cadillac": "Cadillac",
    "chrysler": "Chrysler", "lincoln": "Lincoln", "mitsubishi": "Mitsubishi",
    "volvo": "Volvo", "tesla": "Tesla", "pontiac": "Pontiac", "saturn": "Saturn",
    "scion": "Scion", "mini": "Mini", "jaguar": "Jaguar", "land rover": "Land Rover",
    "porsche": "Porsche", "porche": "Porsche", "polche": "Porsche",
    "genesis": "Genesis",
}
# typos / alias de modelos frecuentes
_MODEL_FIX = {
    "acord": "Accord", "accord": "Accord", "civic": "Civic", "camry": "Camry",
    "carry": "Camry", "camri": "Camry", "camari": "Camry",
    "aldima": "Altima", "oldina": "Altima", "altina": "Altima",
    "altime": "Altima", "altma": "Altima",
    "piloto": "Pilot", "pilote": "Pilot",
    "corolla": "Corolla", "altima": "Altima", "sentra": "Sentra", "versa": "Versa",
    "crv": "CR-V", "cr-v": "CR-V", "rav4": "RAV4", "rav-4": "RAV4",
    "f150": "F-150", "f-150": "F-150", "f250": "F-250", "silverado": "Silverado",
    "tahoe": "Tahoe", "suburban": "Suburban", "pilot": "Pilot", "odyssey": "Odyssey",
    "hodissey": "Odyssey", "odissey": "Odyssey", "odisei": "Odyssey",
    "odysey": "Odyssey", "odisey": "Odyssey", "oddysey": "Odyssey",
    "hodysey": "Odyssey", "odisea": "Odyssey", "odysee": "Odyssey",
    "silverware": "Silverado", "venda": "Venza", "haylander": "Highlander",
    "winstar": "Windstar", "rouge": "Rogue", "4runer": "4Runner",
    "econolen": "Econoline", "econoline": "Econoline", "escarabajo": "Beetle",
    "cierra": "Sierra", "escaley": "Escalade", "caley": "Escalade",
    "escalade": "Escalade", "kona": "Kona", "colorado": "Colorado",
    "promaster": "ProMaster", "soni": "Sonic", "encore": "Encore",
    "avalon": "Avalon", "cayenne": "Cayenne", "terrain": "Terrain",
    "fiesta": "Fiesta", "spectra": "Spectra", "chr": "C-HR",
    "e350": "E350", "e-350": "E350", "e 350": "E350",
    "c300": "C300", "c250": "C250", "c200": "C200",
    "xt4": "XT4", "xt5": "XT5", "xt6": "XT6",
    "gl450": "GL450", "gl550": "GL550", "gla": "GLA",
    "cla": "CLA", "cla250": "CLA 250",
    "gladiador": "Gladiator", "gladiator": "Gladiator", "mojave": "Gladiator",
    "wrangler": "Wrangler", "cherokee": "Cherokee",
    "malibu": "Malibu", "malibú": "Malibu",
    "rogue": "Rogue", "pathfinder": "Pathfinder", "explorer": "Explorer",
    "escape": "Escape", "focus": "Focus", "fusion": "Fusion", "malibu": "Malibu",
    "impala": "Impala", "elantra": "Elantra", "sonata": "Sonata", "optima": "Optima",
    "forte": "Forte", "soul": "Soul", "outback": "Outback", "forester": "Forester",
    "impreza": "Impreza", "jetta": "Jetta", "passat": "Passat", "golf": "Golf",
    "prius": "Prius", "highlander": "Highlander", "siena": "Sienna", "sienna": "Sienna",
    "le sabre": "LeSabre", "lesabre": "LeSabre",
    "tundra": "Tundra", "tacoma": "Tacoma", "4runner": "4Runner",
    "grand cherokee": "Grand Cherokee", "wrangler": "Wrangler", "cherokee": "Cherokee",
    "durango": "Durango", "charger": "Charger", "challenger": "Challenger",
    "1500": "1500", "2500": "2500", "3500": "3500",
    "cx-7": "CX-7", "cx7": "CX-7", "cx-5": "CX-5", "cx5": "CX-5",
    "cx-9": "CX-9", "cx9": "CX-9", "cx-30": "CX-30", "yukon": "Yukon",
    "sierra": "Sierra", "equinox": "Equinox", "traverse": "Traverse",
    "fusion": "Fusion", "mustang": "Mustang", "ranger": "Ranger",
    "300": "300", "sr5": "SR5",
    "s10": "S10", "s-10": "S10", "mazda3": "Mazda3", "mazda 3": "Mazda3",
    "mazda6": "Mazda6", "mazda 6": "Mazda6",
}
# modelo → marca (si el cliente solo dice modelo)
_MODEL_TO_MAKE = {
    "accord": "Honda", "civic": "Honda", "cr-v": "Honda", "pilot": "Honda",
    "odyssey": "Honda", "camry": "Toyota", "corolla": "Toyota", "tacoma": "Toyota",
    "tundra": "Toyota", "rav4": "Toyota", "highlander": "Toyota", "sienna": "Toyota",
    "4runner": "Toyota", "prius": "Toyota", "silverado": "Chevrolet", "tahoe": "Chevrolet",
    "suburban": "Chevrolet", "malibu": "Chevrolet", "impala": "Chevrolet",
    "equinox": "Chevrolet", "traverse": "Chevrolet", "f-150": "Ford", "f-250": "Ford",
    "explorer": "Ford", "escape": "Ford", "focus": "Ford", "fusion": "Ford",
    "mustang": "Ford", "ranger": "Ford", "altima": "Nissan", "sentra": "Nissan",
    "versa": "Nissan", "rogue": "Nissan", "pathfinder": "Nissan",
    "grand cherokee": "Jeep", "wrangler": "Jeep", "cherokee": "Jeep",
    "durango": "Dodge", "charger": "Dodge", "challenger": "Dodge",
    "outback": "Subaru", "forester": "Subaru", "impreza": "Subaru",
    "elantra": "Hyundai", "sonata": "Hyundai", "optima": "Kia", "forte": "Kia",
    "soul": "Kia", "jetta": "Volkswagen", "passat": "Volkswagen", "golf": "Volkswagen",
    "cx-7": "Mazda", "cx-5": "Mazda", "cx-9": "Mazda", "cx-30": "Mazda",
    "yukon": "GMC", "sierra": "GMC", "300": "Chrysler",
    "venza": "Toyota", "highlander": "Toyota", "escalade": "Cadillac",
    "windstar": "Ford", "econoline": "Ford", "beetle": "Volkswagen",
    "colorado": "Chevrolet", "sonic": "Chevrolet", "terrain": "Chevrolet",
    "avalon": "Toyota", "kona": "Hyundai", "encore": "Buick",
    "le sabre": "Buick", "lesabre": "Buick",
    "xt4": "Cadillac", "xt5": "Cadillac", "xt6": "Cadillac",
    "cayenne": "Porsche", "promaster": "Ram", "fiesta": "Ford",
    "s10": "Chevrolet", "s-10": "Chevrolet", "mazda3": "Mazda", "mazda6": "Mazda",
}


_SPANISH_YEAR_WORDS = {
    "cero": 0, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4,
    "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9,
    "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14,
    "quince": 15, "dieciseis": 16, "dieciséis": 16, "diecisiete": 17,
    "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veintiuno": 21,
    "veintiuna": 21, "veintidos": 22, "veintidós": 22, "veintitres": 23,
    "veintitrés": 23, "veinticuatro": 24, "veinticinco": 25,
    "veintiseis": 26, "veintiséis": 26, "veintisiete": 27,
    "veintiocho": 28, "veintinueve": 29,
}


def _expand_spanish_year_words(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        tail = _norm_es(m.group(1) or "cero").strip()
        n = _SPANISH_YEAR_WORDS.get(tail)
        if n is None:
            return m.group(0)
        return f" {2000 + n} "

    return re.sub(
        r"(?i)\bdos\s*mil\s+(cero|uno|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|"
        r"diez|once|doce|trece|catorce|quince|diecis[eé]is|diecisiete|dieciocho|"
        r"diecinueve|veinte|veintiuno|veintiuna|veintid[oó]s|veintitr[eé]s|"
        r"veinticuatro|veinticinco|veintis[eé]is|veintisiete|veintiocho|veintinueve)\b",
        repl,
        text or "",
    )


def _infer_make_from_model(model: str) -> str:
    m = _norm_es(model or "").strip()
    if not m:
        return ""
    for key, make in sorted(_MODEL_TO_MAKE.items(), key=lambda x: -len(x[0])):
        if m == key or m.startswith(key) or key in m:
            return make
    return ""


def _complete_vehicle_fields(lead: dict | None = None, extra: dict | None = None) -> dict:
    """Rellena make desde modelo si falta (Tacoma→Toyota, Grand Cherokee→Jeep)."""
    lead = dict(lead or {})
    extra = extra or {}
    for k in ("year", "make", "model"):
        if extra.get(k):
            lead[k] = extra[k]
    md = (lead.get("model") or "").strip()
    mk = (lead.get("make") or "").strip()
    if md and not mk:
        inferred = _infer_make_from_model(md)
        if inferred:
            lead["make"] = inferred
    # CX-7 a veces queda como "Cx"
    if mk.lower() == "mazda" and re.fullmatch(r"cx-?\d?", md, re.I):
        lead["model"] = "CX-7" if "7" in md or len(md) <= 3 else md.upper().replace("CX", "CX-")
    return lead


_JUNK_MODELS = {
    "benz", "mercedes", "honda", "toyota", "ford", "chevy", "chevrolet",
    "nissan", "dodge", "ram", "gmc", "jeep", "kia", "hyundai", "mazda",
    "wagon", "sedan", "coupe", "suv", "pick", "truck", "troca",
    "clase", "class", "auto", "carro", "camioneta", "jeepeta", "hatchback",
    "sport", "sports", "diesel", "gasolina",
}


def _is_junk_model(model: str | None, make: str | None = None) -> bool:
    """'Benz', 'wagon', 'pickup' no son modelo Safelite."""
    m = _norm_es(model or "").strip()
    if not m or m in _JUNK_MODELS:
        return True
    mk = _norm_es(make or "")
    if mk and (m == mk or m in mk.split()):
        return True
    return False


def _salvage_model_from_chat(psid: str = "", lead: dict | None = None) -> str:
    """Si guardamos Benz/wagon, busca E350 (etc.) en lo que el cliente escribió."""
    lead = lead or {}
    texts: list[str] = []
    try:
        texts.extend(reversed(_hist_texts(lead, "user")[-12:]))
    except Exception:
        pass
    if psid:
        try:
            from .flow import _messenger_user_texts
            texts.extend(_messenger_user_texts(psid))
        except Exception:
            pass
    seen: set[str] = set()
    for t in texts:
        t = (t or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        veh = extract_vehicle_from_text(t)
        md = (veh.get("model") or "").strip()
        mk = veh.get("make") or lead.get("make")
        if md and not _is_junk_model(md, mk):
            return md
    blob = _norm_es(" ".join(seen))
    mm = re.search(r"\b([acdegkls]l?a?)\s*-?\s*(\d{3})\b", blob)
    if mm and re.search(r"\b(mercedes|mercedez|benz)\b", blob):
        return (mm.group(1) + mm.group(2)).upper()
    return ""


def extract_vehicle_from_text(text: str) -> dict:
    """Saca year/make/model del texto si habla del auto. Nunca devuelve address."""
    from .chat_typos import fix_chat_typos

    t = fix_chat_typos((text or "").strip())
    if not t:
        return {}
    t = _expand_spanish_year_words(t)
    # Campos pegados como "marcaxt5.cadillac2026" llegan asi desde Messenger.
    t = re.sub(r"(?i)\bmarca(?=[a-z0-9])", "", t)
    # año pegado al modelo: "Honda hodissey2011" / "4runer97"
    t = re.sub(r"([A-Za-zÁÉÍÓÚáéíóúñÑ])((?:19|20)\d{2})\b", r"\1 \2", t)
    t = re.sub(r"([A-Za-zÁÉÍÓÚáéíóúñÑ]{3,})(\d{2})\b", r"\1 \2", t)
    # dir pura sin palabras de auto → no es vehiculo
    if _looks_like_address(t) and not _VEHICLE_MARKERS.search(t):
        return {}
    low = _norm_es(t)
    out: dict = {}

    # "2.004" / "2,004" / "2.04" (Honda piloto 2.004)
    if not re.search(r"\b((?:19|20)\d{2})\b", t):
        dotted = re.search(r"\b2[\.,](\d{3})\b", t) or re.search(r"\b2[\.,](\d{2})\b", t)
        if dotted:
            ydot = 2000 + int(dotted.group(1))
            if 1950 <= ydot <= date.today().year + 1:
                t = re.sub(re.escape(dotted.group(0)), f" {ydot} ", t, count=1)
                low = _norm_es(t)
    ym = re.search(r"\b((?:19|20)\d{2})\b", t)
    if not ym:
        # "2006el" / "2006elparabrisas" año pegado
        ym = re.search(r"((?:19|20)\d{2})(?=[a-zA-Záéíóúñ]|$)", t)
    if ym:
        y = int(ym.group(1))
        if 1950 <= y <= date.today().year + 1:
            out["year"] = str(y)
    else:
        # año de 2 digitos: "del 15", "civic 08", "modelo 09", "'19"
        y2 = re.search(
            r"(?:\bdel?\b|\bmodelo\b|\baño\b|\bano\b|')\s*(\d{2})\b", low
        ) or re.search(r"\b[a-z]{3,}\s+(\d{2})\b(?!\s*\d)", low)
        if not y2:
            # "99 Chevy tahoe" / "Un 91 gmc" / "4runer97"
            y2 = re.search(r"(?:^|\b(?:un|una|el)\s+)(\d{2})\s+(?=[a-z])", low) or re.search(
                r"([a-z]{3,})(\d{2})\b", low
            )
        if y2:
            n = int(y2.group(y2.lastindex or 1))
            cur = date.today().year % 100
            y = 2000 + n if n <= cur + 1 else 1900 + n
            if 1980 <= y <= date.today().year + 1:
                out["year"] = str(y)

    make_found = None
    make_token = None
    for key, canon in sorted(_MAKE_CANON.items(), key=lambda x: -len(x[0])):
        if re.search(rf"\b{re.escape(key)}\b", low):
            make_found = canon
            make_token = key
            out["make"] = canon
            break

    # marca mal escrita ("hinday", "chevi", "mercedez"): alias de vehicle_norm.
    # Sin esto el lead quedaba SIN auto y nunca llegaba a pedir horarios.
    if not make_found:
        for tok in re.findall(r"[a-z]{3,}", low):
            alias = _MAKE_ALIASES.get(tok)
            if not alias:
                continue
            canon = (alias[0] if isinstance(alias, list) else alias).title()
            make_found = canon
            make_token = tok
            out["make"] = canon
            break

    # modelo: preferir alias conocidos
    model_found = None
    for key, canon in sorted(_MODEL_FIX.items(), key=lambda x: -len(x[0])):
        if re.search(rf"\b{re.escape(key)}\b", low):
            model_found = canon
            out["model"] = canon
            break

    # Ford F-150 / "f 150" / f-250 (Mauricio Limon: "ford 2016 f 150 en ...")
    # El token suelto "f" no entra en _MODEL_FIX ni en el captura-tras-marca.
    m_f = re.search(r"\bf[\s\-]*([1-9][05]0)\b", low)
    if m_f:
        out["model"] = f"F-{m_f.group(1)}"
        model_found = out["model"]
        if not make_found:
            out["make"] = "Ford"
            make_found = "Ford"

    # RAM / Silverado / Sierra 1500 con espacio: "ram 1500", "silverado 2500"
    m_pickup = re.search(r"\b(ram|silverado|sierra)\s*-?\s*(1500|2500|3500)\b", low)
    if m_pickup and not model_found:
        base, num = m_pickup.group(1), m_pickup.group(2)
        out["model"] = f"{base.title()} {num}"
        model_found = out["model"]
        if not make_found:
            out["make"] = {"ram": "Ram", "silverado": "Chevrolet", "sierra": "GMC"}[base]
            make_found = out["make"]

    # CR-V / RAV4 escritos con espacio: "cr v", "rav 4"
    if not model_found:
        if re.search(r"\bcr\s*-?\s*v\b", low):
            out["model"] = "CR-V"
            model_found = "CR-V"
            if not make_found:
                out["make"] = "Honda"
                make_found = "Honda"
        elif re.search(r"\brav\s*-?\s*4\b", low):
            out["model"] = "RAV4"
            model_found = "RAV4"
            if not make_found:
                out["make"] = "Toyota"
                make_found = "Toyota"

    # Mercedes E350 / C300 / GL450 — no guardar "Benz" como modelo
    mm_mb = re.search(r"\b([acdegkls]l?a?)\s*-?\s*(\d{3})\b", low)
    if mm_mb and (
        (make_found and "mercedes" in (make_found or "").lower())
        or re.search(r"\b(mercedes|mercedez|benz)\b", low)
    ):
        out["model"] = (mm_mb.group(1) + mm_mb.group(2)).upper()
        model_found = out["model"]
        out["make"] = "Mercedes Benz"
        make_found = "Mercedes Benz"

    # mazda cx-7 / cx -7 / cx 7 turbo (antes del token suelto "Cx")
    mm_cx = re.search(r"\bcx[\s\-]*(\d{1,2})\b", low)
    if mm_cx:
        out["model"] = f"CX-{mm_cx.group(1)}"
        model_found = out["model"]
        if not make_found:
            out["make"] = "Mazda"
            make_found = "Mazda"

    # si hay marca pero no modelo, intenta token despues de la marca
    if make_found and not model_found:
        # buscar tras el token TAL CUAL lo escribio el cliente ("hinday accent")
        mk_low = _norm_es(make_token or make_found)
        # el año puede ir en medio: "Audi 2015 A4" (caso real que se perdio)
        m = re.search(
            rf"\b{re.escape(mk_low)}\b\s+(?:(?:19|20)\d{{2}}\s+)?([a-z0-9][a-z0-9\-]{{1,20}})",
            low,
        )
        if m:
            tok = m.group(1)
            if tok not in (
                "v6", "v8", "4x4", "awd", "fwd", "sedan", "coupe", "wagon",
                "para", "el", "un", "una", "cx", "king", "benz", "clase",
            ):
                if not re.fullmatch(r"(19|20)\d{2}", tok):
                    out["model"] = _MODEL_FIX.get(tok, tok.title())
    # Ram trims que el cliente dice como si fueran modelo (Barni: long stat)
    if re.search(
        r"\b(lone\s*star|long\s*stat|longhorn|long\s*horn|longstar|"
        r"laramie|big\s*horn|rebel|tradesman|warlock)\b",
        low,
    ):
        if (out.get("make") or "").lower() in ("", "ram", "dodge"):
            out["make"] = out.get("make") or "Ram"
            out["model"] = "Pickup 1500"
            model_found = "Pickup 1500"

    # King Ranch / Super Duty NO son modelo "King" — son F-250/F-350
    if re.search(r"\bking\s*ranch\b|\bsuper\s*duty\b|\bsuperduty\b", low):
        out["make"] = out.get("make") or "Ford"
        if re.search(r"\bf[- ]?350\b|\b350\b", low):
            out["model"] = "F Series F350"
        else:
            out["model"] = "F Series F250"

    # "Cx" suelto con make Mazda → CX-7 por defecto
    if make_found == "Mazda" and re.fullmatch(r"cx-?", _norm_es(out.get("model") or "")):
        out["model"] = "CX-7"
        model_found = "CX-7"

    # inferir marca desde modelo si falta
    if out.get("model") and not out.get("make"):
        inferred = _infer_make_from_model(out["model"])
        if inferred:
            out["make"] = inferred

    # sin marca/año claros y sin markers → no es auto
    if not out.get("year") and not out.get("make") and not _VEHICLE_MARKERS.search(t):
        return {}
    # solo "carro"/"auto" sin datos → no inventar
    if not out.get("year") and not out.get("make") and not out.get("model"):
        return {}
    if out.get("model") and _is_junk_model(out.get("model"), out.get("make")):
        out.pop("model", None)
    if re.search(r"\b(station\s*wagon|wagon|familiar)\b", low):
        out["style"] = "wagon"
    elif re.search(r"\bsedan\b", low):
        out["style"] = "sedan"
    elif re.search(r"\bcoupe|cup[eé]\b", low):
        out["style"] = "coupe"
    elif re.search(r"\bcrew\s*cab\b", low):
        out["style"] = "crew cab"
    return out


def analyze_client_message(text: str, lead: dict | None = None) -> dict:
    """
    ANALISTA duro del mensaje del cliente ANTES de tocar campos o geocode.

    Patrones reales (Ramon Sanchez, Angel Oliva, etc.):
    precio / pago / tecnico / aplazamiento / horario / auto / zip / salida…
    NUNCA mezclar: un modelo no es address; una FAQ no es "manda horarios otra vez".
    """
    t = (text or "").strip()
    lead = lead or {}
    low = _norm_es(t)
    kinds: list[str] = []

    is_opt_out = bool(t) and _client_wants_out(t)
    is_refuse_addr = bool(t) and _client_denies_address(t)

    # pago / como se paga / al tecnico? (Ramon)
    is_payment = bool(
        re.search(
            r"\b(pago|pagar|paga|zelle|apple\s*pay|cash\s*app|venmo|tarjeta|"
            r"efectivo|cash|deposit|como\s+se\s+(ase|hace|paga)|"
            r"al\s+t[eé]cnico|al\s+tecnico|le\s+(boi|voy)\s+a\s+dar|"
            r"le\s+pago\s+al|payment|how\s+do\s+i\s+pay)\b",
            low,
        )
    )
    # solo precio / confirmacion de monto ($210 nada mas?)
    is_price = bool(
        re.search(
            r"\b(precio|presio|predio|costo|coste|vale|cotiz|how much|price|"
            r"cuanto|cuánto|cuanto sale|cuanto cuesta|kero\s+presio|"
            r"reemplaz|reemplazo|nada mas|nada más|fijo|solo eso|210|260|200)\b",
            low,
        )
    ) or bool(re.search(r"\$\s*\d{2,4}", t or ""))
    # aplazamiento / despues te aviso (no es rechazo total)
    is_later = bool(
        re.search(
            r"\b(yo\s+le\s+aviso|te\s+aviso|cuando\s+est[eé]\s+listo|"
            r"cuando\s+tenga(\s+el\s+dinero)?|no\s+estoy\s+listo|"
            r"entre\s+\d+\s*a\s*\d+\s*semanas|\d+\s*a\s*\d+\s*semanas|"
            r"buscando\s+precios\s+por\s+ahora|cualquier\s+cosa\s+le\s+dejo|"
            r"luego\s+te|mas\s+tarde|más\s+tarde|despues\s+te|"
            r"despu[eé]s\s+te|ahorita\s+no|en\s+un\s+rato|"
            r"let\s+you\s+know|i'?ll\s+let|i\s+will\s+let)\b",
            low,
        )
    )
    is_greeting = bool(
        re.fullmatch(
            r"(hola|buenas|buenos dias|buenas tardes|buenas noches|hey|hi|hello|que tal|saludos)[.! ]*",
            low,
        )
    )
    is_phone = bool(_extract_phone(t))
    # nombre corto solo si pedimos contacto
    is_name = False
    if (
        lead.get("slot_id")
        and not lead.get("name")
        and t
        and not is_phone
        and re.fullmatch(r"[A-Za-zÁÉÍÓÚáéíóúñÑ][A-Za-zÁÉÍÓÚáéíóúñÑ\s'\-]{1,40}", t)
        and not _looks_like_address(t)
        and not _looks_like_vehicle_blob(t)
        and not is_price
        and not is_payment
    ):
        is_name = True

    is_schedule = bool(_parse_when_intent(t).get("has_when")) if t else False
    # OJO: no usar "pasado" solo (choca con "no te he pasado la direccion")
    if t and re.search(
        r"\b(lo mas pronto|lo antes|primer hueco|cuando sea|asap|"
        r"manana|mañana|pasado\s*manana|pasado\s*mañana|"
        r"hoy|lunes|martes|miercoles|miércoles|jueves|viernes|sabado|sábado|domingo|"
        r"fin(es)?\s+de\s+semana|el\s+finde|weekend|"
        r"en la (manana|mañana|tarde|noche)|por la (manana|mañana|tarde|noche)|"
        r"que dia|qu[eé]\s+d[ií]a|horario|horarios|disponib)\b",
        low,
    ):
        is_schedule = True
    # FAQ no es "pide horarios de nuevo"
    if is_opt_out or is_refuse_addr or is_payment or is_later:
        is_schedule = False
    # "cuanto cuesta" no es schedule
    if is_price and not re.search(r"\b(hoy|ma[nñ]ana|lunes|martes|sabado|sábado|horario)\b", low):
        # price alone stays price; keep schedule only if day words
        pass

    is_address = bool(t) and _looks_like_address(t) and not _looks_like_vehicle_blob(t)
    # zip solo (5 digitos) cuenta como dir parcial, no como auto
    zip_only = bool(re.fullmatch(r"\s*\d{5}\s*", t or ""))
    if zip_only:
        is_address = True

    veh = extract_vehicle_from_text(t) if t else {}
    # veh REAL = año/marca/modelo en el mensaje. "parabrisas" solo NO es auto.
    has_real_veh = bool(veh.get("year") or veh.get("make") or veh.get("model"))
    is_vehicle = has_real_veh or (
        bool(t)
        and _looks_like_vehicle_blob(t)
        and not is_address
        and not is_price  # "cuanto cuesta el parabrisas" = PRECIO, no auto
        and not is_payment
    )
    # si hay precio + solo palabra parabrisas sin year/make/model → no es vehicle
    if is_price and not has_real_veh:
        is_vehicle = False
        veh = {}

    # prioridad: si es blob de auto con año/marca, NUNCA es address
    if is_vehicle and not (_STREET_MARKERS.search(t or "") and re.search(r"\b\d{1,6}\s+[A-Za-z]", t or "")):
        is_address = False

    is_service = bool(detect_service_pref(t)) if t else False
    is_photo = "[el cliente envio foto" in (t or "").lower() or bool(
        re.search(r"\b(foto|imagen|mira|ves el|te mando)\b", low)
    )

    if is_opt_out:
        kinds.append("opt_out")
    if is_refuse_addr:
        kinds.append("refuse_address")
    if is_payment:
        kinds.append("payment")
    if is_later:
        kinds.append("later")
    if is_price:
        kinds.append("price")
    if is_vehicle:
        kinds.append("vehicle")
    if is_address:
        kinds.append("address")
    if is_phone:
        kinds.append("phone")
    if is_name:
        kinds.append("name")
    if is_schedule:
        kinds.append("schedule")
    if is_service:
        kinds.append("service_mode")
    if is_greeting:
        kinds.append("greeting")
    if is_photo:
        kinds.append("photo")
    if not kinds:
        kinds.append("chat")

    # campos permitidos este turno (gate anti-mezcla)
    allow: set[str] = set()
    if is_vehicle:
        allow |= {"year", "make", "model", "style", "glass", "vehicle_size"}
    if is_address:
        allow |= {"address", "zip", "city", "state"}
    if is_phone:
        allow |= {"phone", "client_phone"}
    if is_name:
        allow |= {"name"}
    if is_service:
        allow |= {"service_mode"}
    if is_opt_out:
        allow |= {"stage"}
    # NUNCA address si este mensaje es auto y no dir
    if is_vehicle and not is_address:
        allow -= {"address", "zip", "city", "state"}
    # FAQ / later: no tocar campos
    if is_payment or is_later:
        allow.clear()

    # primary: PRECIO gana si preguntan cuanto (aunque digan parabrisas)
    if is_opt_out:
        primary = "opt_out"
    elif is_refuse_addr:
        primary = "refuse_address"
    elif is_payment:
        primary = "payment"
    elif is_later:
        primary = "later"
    elif is_price and not has_real_veh:
        primary = "price"  # "cuanto cuesta parabrisas" → PRECIO ya, no rodeos
    elif is_vehicle and has_real_veh:
        primary = "vehicle"
    elif is_address:
        primary = "address"
    elif is_schedule:
        primary = "schedule"
    elif is_price:
        primary = "price"
    elif is_greeting:
        primary = "greeting"
    else:
        primary = kinds[0] if kinds else "chat"

    analysis = {
        "kinds": kinds,
        "primary": primary,
        "is_vehicle": is_vehicle,
        "is_address": is_address,
        "is_price": is_price,
        "is_payment": is_payment,
        "is_later": is_later,
        "is_phone": is_phone,
        "is_name": is_name,
        "is_schedule": is_schedule,
        "is_opt_out": is_opt_out,
        "is_refuse_address": is_refuse_addr,
        "is_greeting": is_greeting,
        "is_photo": is_photo,
        "vehicle": veh,
        "phone": _extract_phone(t) if is_phone else "",
        # el zip se extrae SIEMPRE: antes solo se miraba si el mensaje se
        # clasificaba como direccion, y se perdian casos como
        # "2015 Toyota Tundra 29349" (auto + zip en el mismo mensaje).
        "zip": _extract_zip(t),
        "address_raw": t[:160] if is_address else "",
        "allow_fields": sorted(allow),
        "raw_preview": t[:120],
    }
    log.info(
        "ANALYZE primary=%s kinds=%s allow=%s veh=%s addr=%s | %r",
        primary,
        kinds,
        sorted(allow),
        {k: veh.get(k) for k in ("year", "make", "model") if veh.get(k)},
        bool(is_address),
        t[:80],
    )
    return analysis


def _already_showed_slots(lead: dict | None) -> bool:
    """True si ya mandamos lista de horarios (no re-spamear como Ramon Sanchez)."""
    lead = lead or {}
    for msg in _hist_texts(lead, "assistant")[-10:]:
        if re.search(
            r"(?i)(cu[aá]l te sirve|n[uú]mero o d[ií]a|"
            r"env[ií]o a domicilio|horarios en taller|"
            r"horarios en que podemos ir|lo m[aá]s pronto|"
            r"qu[eé] d[ií]a te acomoda|te dejamos anotado|"
            r"•\s*\w+.*(?:am|pm))",
            msg or "",
        ):
            return True
    return False


def _soft_day_cta(lead: dict | None = None, lang: str = "es") -> str:
    """CTA corto sin re-listar horarios."""
    if _already_showed_slots(lead):
        return _t(
            lang,
            " ¿Qué día te acomoda de los que te pasé?",
            " Which of the days I sent works for you?",
        )
    return _t(
        lang,
        " " + ASK_ZIP_ES,
        " " + ASK_ZIP_EN,
    )


def _is_non_windshield_glass(text: str) -> bool:
    """Techo / trasero / laterales: no cotizar como parabrisas $210 genérico."""
    low = _norm_es(text or "")
    return bool(
        re.search(
            r"\b(techo|sunroof|panor[aá]mic|moonroof|cristal del techo|"
            r"trasero|vidrio trasero|medall[oó]n|medallon|"
            r"lateral|ventanilla|quarter|puerta|"
            r"rear\s+(glass|window)|side\s+window|back\s+glass)\b",
            low,
        )
    )


def _siguiente_paso(lead: dict, lang: str) -> str:
    """Reencauce corto segun lo que falte: auto -> zip -> elegir horario."""
    if not _has_vehicle(lead):
        return " " + _next_missing_ask(lead, lang=lang)
    if not _lead_zip(lead):
        return " " + _ask_zip_now(lead)
    if _already_showed_slots(lead):
        return _soft_day_cta(lead, lang)
    return _t(lang, " ¿Te paso los horarios en que podemos ir a tu lugar?",
              " Want me to pull the times we can come to you?")


def _faq_extra_reply(lead: dict, text: str, lang: str) -> str | None:
    """
    Responde la pregunta del cliente y reencauza en la misma frase.

    Antes estas preguntas caian en 'chat' generico y el bot contestaba
    "Claro, ¿que carro tienes?" — la misma frase repetida hasta que el
    cliente se iba. No se inventan datos que el negocio no tiene cargados
    (duracion, seguro, marca del vidrio): eso lo confirma el agente.
    """
    clave = _faq_extra_detectar(text or "")
    if not clave:
        return None

    if clave == "humano":
        # Esta pregunta suele ser una forma de iniciar la cotización. Mantener
        # el flujo activo en vez de silenciar el chat y dejar al cliente esperando.
        return _siguiente_paso(lead, lang)

    if clave == "negocio":
        if not _has_vehicle(lead):
            return _t(
                lang,
                "Sí, vamos a domicilio. ¿Qué marca, modelo y año es tu vehículo?",
                "Yes, we come to you. What make, model, and year is your vehicle?",
            )
        if not _lead_zip(lead):
            return _reply_vehicle_short(lead, text)
        details = _job_details_line(lead, lang)
        extra = f" Para tu caso: {details}." if details else ""
        base = _t(
            lang,
            f"Cambiamos el parabrisas a domicilio.{extra}",
            f"We replace the windshield at your place.{extra}",
        )
    elif clave == "ubicacion":
        base = _t(
            lang,
            "Servicio en los 50 estados.",
            "Service in all 50 states.",
        )
    elif clave == "domicilio":
        base = _t(
            lang,
            "Sí, vamos a domicilio.",
            "Yes, we come to you.",
        )
    elif clave == "telefono":
        base = _t(
            lang,
            f"Sí. WhatsApp: {os.getenv('BUSINESS_WHATSAPP', 'tu número de WhatsApp')}.",
            f"Yes. WhatsApp: {os.getenv('BUSINESS_WHATSAPP', 'your business WhatsApp number')}.",
        )
    elif clave == "duracion":
        base = _t(
            lang,
            "Es el mismo día.",
            "Same-day service.",
        )
    elif clave == "seguro":
        base = _t(
            lang,
            "Lo del seguro te lo revisa el agente al cerrar.",
            "An agent reviews the insurance part when closing.",
        )
    elif clave == "reparar":
        base = _t(
            lang,
            "Eso te lo confirma el agente según cómo esté el vidrio.",
            "An agent confirms that based on how the glass looks.",
        )
    elif clave == "calidad":
        base = _t(
            lang,
            "El vidrio va con garantía en el trabajo; el agente te confirma el detalle.",
            "The glass comes with warranty on the work; an agent confirms details.",
        )
    else:  # requisitos
        base = _t(
            lang,
            "Claro, te ayudo a revisarlo.",
            "Of course, I can help check that.",
        )

    paso = _siguiente_paso(lead, lang)
    return (base + paso).strip()


def reply_like_human(lead: dict, text: str, analysis: dict | None = None) -> str | None:
    """
    Router de FAQ / mensajes cortos (vendedor real).
    Si devuelve str → usar eso y NO re-spamear horarios ni llamar al LLM.
    Aprendizajes: Ramon (precio/pago), Angel (urgencia), ubicación, techo, saludo.
    """
    analysis = analysis or {}
    lang = _client_lang(lead, text, psid=lead.get("psid"))
    primary = analysis.get("primary") or ""
    low = _norm_es(text or "")
    pl = price_line(
        lead.get("year"), lead.get("make") or "", lead.get("model") or "",
        text, lead=lead, psid=lead.get("psid"), lang=lang,
    )

    # CRISTAL NO PARABRISAS → humano (no cotizar 210)
    if _is_non_windshield_glass(text or "") and not re.search(
        r"\b(parabrisas|windshield|frente|front)\b", low
    ):
        return _t(
            lang,
            "Eso no es parabrisas de frente — te lo ve un agente en un momento y te cotiza bien.",
            "That's not a front windshield — an agent will check and quote you properly in a moment.",
        )

    # PAGO / al tecnico?
    if analysis.get("is_payment") or primary == "payment":
        if re.search(r"\b(t[eé]cnico|tecnico|instal)\b", low):
            return _t(
                lang,
                "No — el pago es a admin (Zelle, Apple Pay o Cash App), no al técnico.",
                "No — payment goes to admin (Zelle, Apple Pay or Cash App), not the tech.",
            ) + (_soft_day_cta(lead, lang) if _already_showed_slots(lead) else "")
        return _t(
            lang,
            "El pago es como te sea más fácil: Zelle, Apple Pay o Cash App — va a admin.",
            "Payment however is easiest for you: Zelle, Apple Pay or Cash App — to admin.",
        ) + (_soft_day_cta(lead, lang) if _already_showed_slots(lead) else "")

    # DESPUES TE AVISO
    if analysis.get("is_later") or primary == "later":
        return _t(
            lang,
            "Perfecto, cuando estés listo me escribes y te busco horarios.",
            "Perfect — when you're ready, message me and I'll lock a day.",
        )

    # PRECIO — hasta el zip no se cotiza. Después el flujo manda horarios.
    if analysis.get("is_price") or primary == "price":
        if not _lead_zip(lead):
            return _reply_vehicle_short(lead, text) if _has_vehicle(lead) else _next_missing_ask(lead, text=text)
        return _reply_price_short(lead, text)

    # SOLO SALUDO
    if analysis.get("is_greeting") or primary == "greeting":
        # si solo es saludo (sin auto/zip/precio en el mismo msg)
        if not analysis.get("is_vehicle") and not analysis.get("is_address") and not analysis.get("is_price"):
            return _reply_greeting_short(lead, text)

    # Preguntas legitimas que antes caian en 'chat' y hacian repetir
    # "¿que carro tienes?": ubicacion, domicilio, telefono, seguro...
    # Va DESPUES de precio: "¿cuanto cuesta reparar?" es precio, no FAQ.
    extra = _faq_extra_reply(lead, text or "", lang)
    if extra:
        return extra

    try:
        from .aprender import suggest_reply

        learned = suggest_reply(lead, text or "")
        if learned:
            return learned
    except Exception:
        pass

    # Pregunta sin regla propia pero que un humano YA contesto asi antes
    # (>=2 veces), aprendido de las conversaciones reales del inbox.
    try:
        from .aprender import human_answer_for

        human_reply = human_answer_for(text or "")
        if human_reply:
            return human_reply
    except Exception:
        pass

    # GARANTIA
    if re.search(r"\b(garant[ií]a|warranty|incluido|incluye)\b", low) and not analysis.get("is_price"):
        base = _t(
            lang,
            f"Sí, {pl} con garantía en el trabajo. Nada más.",
            f"Yes, {pl} with warranty on the work. That's it.",
        )
        if _already_showed_slots(lead):
            return base + _soft_day_cta(lead, lang)
        if _has_vehicle(lead) and not _lead_zip(lead):
            return base + _t(lang, " ¿Zip y te busco día?", " Zip and I'll find a day?")
        return base

    # FOTO / VIN → avanzar, no reiniciar
    if analysis.get("is_photo") or re.search(r"\b(vin|n[uú]mero de serie)\b", low):
        if _has_vehicle(lead):
            return _reply_vehicle_short(lead, text)
        return _t(
            lang,
            "Vi la foto del cristal. Te ayudamos con el cambio a domicilio. ¿Qué marca, modelo y año es tu vehículo?",
            "I saw the windshield photo. We can help with mobile replacement. What make, model, and year is your vehicle?",
        )

    # GRACIAS / ok corto ya con horarios
    if _already_showed_slots(lead) and re.fullmatch(
        r"(ok|okay|va|vale|gracias|thank(s| you)?|perfecto|listo|super|sí|si)[.! ]*",
        low,
    ):
        return _t(
            lang,
            "Perfecto. Cuando elijas día me escribes y te busco horarios.",
            "Perfect. When you pick a day, message me and we'll note it.",
        )

    return None


def _address_gaps(lead: dict) -> list[str]:
    """Que le falta a la direccion (vacio = completa o ni empezada).

    - sin nada → [] y how_to_ask_address = 'simple'
    - parcial → lista corta de lo que falta (zip, calle_numero, etc.)
    """
    addr = (lead.get("address") or "").strip()
    # basura (auto guardado como dir) = como si no hubiera address
    if addr and not _looks_like_address(addr):
        addr = ""
    zipc = (lead.get("zip") or _extract_zip(addr) or "").strip()
    city = (lead.get("city") or "").strip()
    state = (lead.get("state") or "").strip()
    gaps: list[str] = []
    if not addr and not zipc and not city:
        return []  # todavia no pidio / no dio nada → pedir solo "dame tu direccion"
    # dio algo: ver huecos
    # calle con numero (minimo util para mobile) — no usar año de auto (19xx/20xx solo)
    has_street_num = bool(_looks_like_address(addr)) or bool(
        re.search(r"\b\d{1,6}\s+[A-Za-zÁÉÍÓÚáéíóúñÑ]{2,}", addr)
        and not re.fullmatch(r"(?i).*\b(19|20)\d{2}\b.*", addr)  # año solo no cuenta
    )
    if not has_street_num and addr and _STREET_MARKERS.search(addr) and re.search(r"\d", addr):
        has_street_num = True
    if not addr or not has_street_num:
        # si solo mandó zip o ciudad
        if zipc and not addr:
            gaps.append("calle_y_numero")
        elif addr and not has_street_num:
            gaps.append("calle_y_numero")
        elif not addr:
            gaps.append("calle_y_numero")
    if not zipc:
        gaps.append("zip")
    # city/state solo si no hay zip (con zip Safelite resuelve)
    if not zipc and not city:
        gaps.append("ciudad")
    if not zipc and not state and city:
        gaps.append("estado")
    return gaps


def _how_to_ask_address(lead: dict) -> str:
    """El bot NUNCA pide calle. Solo ZIP para horarios; cierre manual del humano."""
    zipc = _lead_zip(lead)
    if not _has_vehicle(lead):
        return "falta auto: pide año marca modelo. NO pidas dir/calle/tel."
    if not zipc:
        return "para HORARIOS pedi SOLO el código postal (5 digitos). Di 'código postal', NO 'zip'. PROHIBIDO pedir calle/nombre/tel."
    return "código postal ok: muestra horarios. PROHIBIDO pedir calle, direccion, nombre o telefono. Contacto = llamarnos por Facebook."


def _missing(lead: dict) -> list[str]:
    """Solo lo del bot: auto → zip → (opcional) elija dia. Sin calle ni contacto."""
    need = []
    if not (lead.get("year") and lead.get("make") and lead.get("model")):
        need.append("auto (año marca modelo)")
    if not _lead_zip(lead):
        need.append("zip (5 digitos — para horarios)")
    if _slots_ready(lead) and not lead.get("slot_id"):
        need.append("horario (que elija dia/hora de los mostrados)")
    return need


def _core_ready(lead: dict) -> bool:
    """Listo para HORARIOS LIVE = auto + ZIP."""
    return _slots_ready(lead)


def _has_slot(lead: dict) -> bool:
    return bool(lead.get("slot_id") and lead.get("slot_date"))


def _ready(lead: dict) -> bool:
    """
    Bot NO agenda solo (cierre manual). Siempre False para try_book automatico.
    El humano completa calle/contacto y cierra.
    """
    return False


_DIAS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
_MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]
_DIAS_MAP = {
    "lunes": 0, "martes": 1, "miercoles": 2, "miércoles": 2,
    "jueves": 3, "viernes": 4, "sabado": 5, "sábado": 5, "domingo": 6,
}


def _norm_es(s: str) -> str:
    s = (s or "").lower().strip()
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ü", "u"), ("ñ", "n")):
        s = s.replace(a, b)
    return s


def _fmt_day(iso_date: str) -> str:
    try:
        d = datetime.strptime(iso_date[:10], "%Y-%m-%d")
        return f"{_DIAS[d.weekday()]} {d.day} de {_MESES[d.month - 1]}"
    except Exception:
        return iso_date


def _fmt_hour(hhmm: str) -> str:
    try:
        h, m = hhmm.split(":")[:2]
        h = int(h)
        ap = "am" if h < 12 else "pm"
        h12 = h % 12 or 12
        return f"{h12}:{m}{ap}" if m != "00" else f"{h12}{ap}"
    except Exception:
        return hhmm


def _slot_period(start: str) -> str:
    try:
        h = int(str(start).split(":")[0])
    except Exception:
        return "tarde"
    if h < 12:
        return "manana"
    if h < 17:
        return "tarde"
    return "noche"


def detect_service_pref(text: str) -> str | None:
    """
    domicilio vs taller.
    Aprendizaje Angel Oliva: "yo lo yebo/llevo" = TALLER aunque antes diga domicilio.
    """
    t = _norm_es(text or "")
    # llevar el carro = taller (aunque diga domicilio en el mismo chat)
    if re.search(
        r"\b(sucursal|tienda|local|taller|"
        r"lo\s+(llevo|yebo|llebo|llebo)|yo\s+lo\s+(llevo|yebo|llebo)|"
        r"si\s+yo\s+lo\s+(llevo|yebo)|"
        r"llevar(me)?\s+el\s+(carro|auto|camion)|paso\s+yo|voy\s+yo|"
        r"lo\s+puedo\s+llevar|puedo\s+llevar|"
        r"ir\s+(a|al)\s+(la\s+)?(sucursal|tienda|local|taller)|"
        r"in.?store|shop|drop\s*off)\b",
        t,
    ):
        return "sucursal"
    if re.search(
        r"\b(domicilio|a\s+(mi\s+)?casa|a\s+mi\s+direccion|vayan|vengan|vienen|"
        r"mobile|en\s+casa|donde\s+vivo|donde\s+estoy|"
        r"envio\s+a\s+domicilio|vengan\s+ustedes)\b",
        t,
    ):
        return "domicilio"
    return None


def _wants_urgency_today(text: str) -> bool:
    t = _norm_es(text or "")
    return bool(
        re.search(
            r"\b(hoy|today|lo\s+necesito\s+hoy|para\s+hoy|es\s+hoy|"
            r"urgente|asap|ya\s+mismo|necesito\s+poner|poner\s+hoy)\b",
            t,
        )
    )


def _client_accepts_taller_manana(text: str, lead: dict | None = None) -> bool:
    """
    Angel Oliva: dijo domicilio, luego urgencia HOY, humano ofreció mañana TALLER,
    cliente: 'Ok para mañana' / 'si yo lo yebo' → es TALLER, no domicilio.
    """
    t = _norm_es(text or "")
    if detect_service_pref(text) == "sucursal":
        return True
    # ok/si + mañana sin insistir domicilio
    accepts = bool(
        re.search(
            r"\b(ok|okay|va|dale|si|sí|de\s+una|perfecto|listo|esta\s+bien|"
            r"para\s+ma[nñ]ana|ma[nñ]ana\s+(aq|a\s+que|a\s+qu[eé]|que|podemos))\b",
            t,
        )
    )
    if not accepts:
        return False
    if re.search(r"\b(domicilio|a\s+casa|vengan|vayan)\b", t):
        return False
    # si en el chat reciente el bot/humano ofreció taller
    lead = lead or {}
    for msg in _hist_texts(lead, "assistant")[-6:]:
        if re.search(r"(?i)\b(taller|si usted lo lleva|si lo lleva|en taller|sucursal)\b", msg or ""):
            return True
    # si el lead ya quedó en sucursal
    if (lead.get("service_mode") or "").lower() == "sucursal":
        return True
    return False


def _parse_when_intent(text: str) -> dict:
    """Interpreta dia/hora en lenguaje natural (sin menus)."""
    t = _norm_es(text or "")
    t = re.sub(r"([a-z])(\d)", r"\1 \2", t)
    t = re.sub(r"(\d)([a-z])", r"\1 \2", t)
    t = re.sub(r"\s+", " ", t).strip()
    intent: dict = {
        "weekday": None,
        "date": None,
        "period": None,
        "hour": None,
        "relative": None,
        "has_when": False,
    }
    today = date.today()

    # 1) Periodo del dia PRIMERO (para no confundir "en la manana" con el dia "manana")
    if re.search(r"\b((por|en|de)\s+la\s+manana|a\s+la\s+manana|media\s+manana|morning|"
                 r"temprano|tempranito|early|primera\s+hora)\b", t):
        intent["period"] = "manana"
        intent["has_when"] = True
    elif re.search(r"\b((por|en|de)\s+la\s+tarde|a\s+la\s+tarde|afternoon|evening|"
                   r"despues\s+del\s+trabajo|after\s+work|saliendo\s+del\s+trabajo)\b", t):
        intent["period"] = "tarde"
        intent["has_when"] = True
    elif re.search(r"\b((por|en|de)\s+la\s+noche|evening|night)\b", t):
        intent["period"] = "noche"
        intent["has_when"] = True

    # Texto sin frases de periodo, para detectar "manana" = dia siguiente
    t_day = re.sub(r"\b(por|en|de|a)\s+la\s+(manana|tarde|noche)\b", " ", t)
    t_day = re.sub(r"\b(morning|afternoon|evening|night)\b", " ", t_day)
    t_day = re.sub(r"\s+", " ", t_day).strip()

    if re.search(r"\b(hoy|today)\b", t_day):
        intent["date"] = today.isoformat()
        intent["relative"] = "hoy"
        intent["has_when"] = True
    elif re.search(r"\b(pasado\s*manana)\b", t_day):
        intent["date"] = (today + timedelta(days=2)).isoformat()
        intent["relative"] = "pasado_manana"
        intent["has_when"] = True
    elif re.search(r"\b(manana|tomorrow)\b", t_day):
        # "manana" solo = dia siguiente (ya sacamos "en la manana")
        intent["date"] = (today + timedelta(days=1)).isoformat()
        intent["relative"] = "manana"
        intent["has_when"] = True

    for name, wd in _DIAS_MAP.items():
        if re.search(rf"\b{re.escape(_norm_es(name))}\b", t):
            intent["weekday"] = wd
            intent["has_when"] = True
            break

    # "el 25" / "dia 25" / "25 de julio" — evitar capturar horas sueltas
    mday = re.search(
        r"\b(\d{1,2})\s+de\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)\b",
        t,
    ) or re.search(r"\b(?:dia|el\s+dia)\s+(\d{1,2})\b", t)
    if mday:
        try:
            day_n = int(mday.group(1))
            mes = today.month
            if mday.lastindex and mday.lastindex >= 2 and mday.group(2):
                mes = _MESES.index(_norm_es(mday.group(2))) + 1
            for o_year_off in (0, 1):
                y = today.year + o_year_off
                try:
                    cand = date(y, mes, day_n)
                except ValueError:
                    continue
                if cand >= today:
                    intent["date"] = cand.isoformat()
                    intent["has_when"] = True
                    break
        except Exception:
            pass

    # hora exacta: 10am, 3 pm, 15:00, a las 10 — no capturar dias del mes sueltos sin am/pm/las
    mh = re.search(
        r"\b(?:a\s+las?\s+)(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.?m\.?|p\.?m\.?)?\b",
        t,
    ) or re.search(
        r"\b(\d{1,2})(?::(\d{2}))\s*(am|pm|a\.?m\.?|p\.?m\.?)?\b",
        t,
    ) or re.search(
        r"\b(\d{1,2})\s*(am|pm|a\.?m\.?|p\.?m\.?)\b",
        t,
    )
    if mh:
        h = int(mh.group(1))
        ap = ""
        if mh.lastindex and mh.lastindex >= 3:
            ap = (mh.group(3) or "").lower().replace(".", "")
        elif mh.lastindex and mh.lastindex >= 2 and mh.group(2) and not str(mh.group(2)).isdigit():
            ap = (mh.group(2) or "").lower().replace(".", "")
        if ap.startswith("p") and h < 12:
            h += 12
        elif ap.startswith("a") and h == 12:
            h = 0
        elif not ap and 1 <= h <= 7:
            h += 12
        if 0 <= h <= 23:
            intent["hour"] = h
            intent["has_when"] = True
            if intent["period"] is None:
                intent["period"] = "manana" if h < 12 else ("tarde" if h < 17 else "noche")

    if re.search(r"\b(lo mas pronto|lo antes|cuando sea|cualquiera|el primero|primer hueco|lo que haya)\b", t):
        intent["relative"] = intent["relative"] or "asap"
        intent["has_when"] = True

    # Si dijo un dia de la semana + "en la manana", no forzar fecha de "manana"=tomorrow
    # (ya filtrado). Si quedaron date+weekday y chocan, priorizar weekday del cliente.
    return intent


def _slot_matches_day(slot: dict, intent: dict) -> bool:
    try:
        d = datetime.strptime(slot["date"][:10], "%Y-%m-%d").date()
    except Exception:
        return False
    ok_date = True
    ok_wd = True
    if intent.get("date"):
        ok_date = slot["date"][:10] == intent["date"][:10]
    if intent.get("weekday") is not None:
        ok_wd = d.weekday() == intent["weekday"]
    # si pidio ambos y no calzan juntos, preferir el dia de la semana (mas comun en chat)
    if intent.get("date") and intent.get("weekday") is not None:
        if ok_date and ok_wd:
            return True
        if ok_wd and not ok_date:
            return True
        return ok_date
    if intent.get("date"):
        return ok_date
    if intent.get("weekday") is not None:
        return ok_wd
    return True


def _score_slot(slot: dict, intent: dict) -> int:
    score = 0
    try:
        sh = int(str(slot.get("startTime") or "12").split(":")[0])
    except Exception:
        sh = 12
    period = slot.get("period") or _slot_period(slot.get("startTime") or "")

    if intent.get("date") and slot["date"][:10] == intent["date"][:10]:
        score += 50
    if intent.get("weekday") is not None:
        try:
            if datetime.strptime(slot["date"][:10], "%Y-%m-%d").weekday() == intent["weekday"]:
                score += 40
        except Exception:
            pass
    if intent.get("period") and period == intent["period"]:
        score += 25
    if intent.get("hour") is not None:
        score += max(0, 20 - abs(sh - intent["hour"]) * 3)
    if intent.get("relative") == "asap":
        score += max(0, 15 - len(slot.get("date") or ""))  # prefer earlier dates via sort
    return score


def _ultimo_msg_bot(lead: dict | None) -> str:
    """Ultimo texto que el bot le mando a este lead (para anclar el 'elegi el 2')."""
    if not lead:
        return ""
    hist = lead.get("history")
    if isinstance(hist, str):
        try:
            hist = json.loads(hist or "[]")
        except Exception:
            return ""
    if not isinstance(hist, list):
        return ""
    for m in reversed(hist[-8:]):
        if isinstance(m, dict) and m.get("role") == "assistant":
            return m.get("content") or ""
    return ""


def _msgs_bot(lead: dict | None, n: int = 6) -> list[str]:
    """Ultimos n mensajes del bot, del mas reciente al mas viejo."""
    if not lead:
        return []
    hist = lead.get("history")
    if isinstance(hist, str):
        try:
            hist = json.loads(hist or "[]")
        except Exception:
            return []
    if not isinstance(hist, list):
        return []
    out = []
    for m in reversed(hist[-16:]):
        if isinstance(m, dict) and m.get("role") == "assistant":
            out.append(m.get("content") or "")
            if len(out) >= n:
                break
    return out


def _opciones_ancladas(lead: dict | None, options: list[dict]) -> list[dict]:
    """
    Lista numerada que el cliente tiene realmente delante.

    Mira hacia atras y no solo al ultimo mensaje: si el cliente pregunta algo
    en medio ("como se hace el pago?") el bot contesta sin lista, y un "1"
    despues se quedaba sin anclaje. opciones_mostradas ya valida cada etiqueta
    contra la disponibilidad actual, asi que una lista vieja que ya no existe
    no matchea y devolvemos vacio (preferimos no agendar que agendar mal).
    """
    for msg in _msgs_bot(lead):
        vistas = _opciones_mostradas(msg, options)
        if vistas:
            return vistas
    return []


def interpret_slot_request(text: str, options: list[dict], lead: dict | None = None) -> dict:
    """
    Interpreta lo que el cliente dice y lo cruza con disponibilidad real.
    status: matched | need_clarify | day_unavailable | time_unavailable | no_intent | empty | taller_offer
    lead opcional: para ofrecer taller cuando HOY/MAÑANA no hay domicilio.
    """
    if not options:
        return {"status": "empty", "slot": None, "message": None, "candidates": []}

    # 1) eleccion por numero sobre la lista que se le mostro ("1", "el 2",
    #    "la primera"). Era el hueco mas caro: 0 de 14 lograban elegir.
    #    Se ancla al ultimo mensaje del bot para que el numero apunte SIEMPRE
    #    al horario que el cliente tenia enfrente.
    mostradas = _opciones_ancladas(lead, options)
    if mostradas:
        elegido = _parse_num_choice(text or "", mostradas, limit=len(mostradas))
        if not elegido:
            # Casi nadie contesta "2". Contestan "el sabado", "el de arriba",
            # "el de las 12", "el 10 de agosto", "monday works". Se busca sobre
            # la lista que el cliente tiene delante, asi que no puede salir un
            # hueco que no vio.
            elegido = _slot_parse_natural(text or "", mostradas)
        # Un "ok"/"dale" suelto NO elige horario. Solo un numero o un dia/hora
        # explicito del cliente puede anotar una preferencia. Antes esto
        # convertia cualquier "ok" en "te dejo jueves 10" sin que el cliente
        # lo hubiera dicho.
        if elegido:
            return {"status": "matched", "slot": elegido, "message": None, "candidates": []}

    # 2) "cualquiera" / "el que sea" / "esta semana" → lo mas pronto
    if _es_flexible(text or ""):
        return {
            "status": "matched",
            "slot": _ventanas_unicas(options, limit=1)[0],
            "message": None,
            "candidates": [],
        }

    intent = _parse_when_intent(text or "")
    # sin indicios de dia/hora → no forzar match
    if not intent.get("has_when"):
        return {"status": "no_intent", "slot": None, "message": None, "candidates": options}

    # filtro por dia si lo pidio
    day_filtered = [o for o in options if _slot_matches_day(o, intent)]
    # si date+weekday chocaron y no hubo match, reintentar solo weekday
    if not day_filtered and intent.get("weekday") is not None and intent.get("date"):
        intent_wd = dict(intent)
        intent_wd["date"] = None
        intent_wd["relative"] = None
        day_filtered = [o for o in options if _slot_matches_day(o, intent_wd)]
        if day_filtered:
            intent = intent_wd
    if intent.get("date") or intent.get("weekday") is not None:
        if not day_filtered:
            alts = options[:5]
            return {
                "status": "day_unavailable",
                "slot": None,
                "message": _msg_day_unavailable(intent, alts, lead=lead, user_text=text or ""),
                "candidates": alts,
            }
    else:
        day_filtered = list(options)

    # filtro por periodo/hora
    timed = day_filtered
    if intent.get("period") or intent.get("hour") is not None:
        better = []
        for o in day_filtered:
            if intent.get("period") and (o.get("period") or _slot_period(o.get("startTime") or "")) != intent["period"]:
                if intent.get("hour") is None:
                    continue
            if intent.get("hour") is not None:
                try:
                    sh = int(str(o.get("startTime") or "12").split(":")[0])
                except Exception:
                    sh = 12
                if abs(sh - intent["hour"]) > 3:
                    continue
            better.append(o)
        if better:
            timed = better
        elif intent.get("period") or intent.get("hour") is not None:
            # dia ok pero hora no
            return {
                "status": "time_unavailable",
                "slot": None,
                "message": _msg_time_unavailable(intent, day_filtered),
                "candidates": day_filtered,
            }

    # rank
    ranked = sorted(timed, key=lambda o: (-_score_slot(o, intent), o["date"], o.get("startTime") or ""))
    if not ranked:
        return {
            "status": "day_unavailable",
            "slot": None,
            "message": _msg_day_unavailable(intent, options[:5], lead=lead, user_text=text or ""),
            "candidates": options[:5],
        }

    top = ranked[0]
    # si solo dijo el dia y hay varias ventanas ese dia → aclarar elegante
    same_day = [o for o in day_filtered if o["date"] == top["date"]]
    only_day = (intent.get("date") or intent.get("weekday") is not None) and not intent.get("period") and intent.get("hour") is None
    if only_day and len(same_day) > 1 and intent.get("relative") != "asap":
        return {
            "status": "need_clarify",
            "slot": None,
            "message": _msg_clarify_day(top["date"], same_day),
            "candidates": same_day,
        }

    return {
        "status": "matched",
        "slot": top,
        "message": None,
        "candidates": ranked[:3],
    }


def _human_intent_day(intent: dict) -> str:
    if intent.get("relative") == "hoy":
        return "hoy"
    if intent.get("relative") == "manana":
        return "mañana"
    if intent.get("relative") == "pasado_manana":
        return "pasado mañana"
    if intent.get("date"):
        return _fmt_day(intent["date"])
    if intent.get("weekday") is not None:
        return _DIAS[intent["weekday"]]
    return "ese dia"


def _fmt_slot_bullet(o: dict) -> str:
    return f"• {o['label']}"


def _list_slots_elegant(options: list[dict], limit: int = SLOT_MAX_OPCIONES) -> str:
    """
    Lista NUMERADA de ventanas. El numero es lo que el cliente responde;
    `opciones_mostradas` lo vuelve a cruzar con estas mismas etiquetas.
    """
    texto, _ = _lista_numerada(options, limit=limit)
    return texto


def _group_slots_by_day(options: list[dict]) -> list[tuple[str, list[dict]]]:
    """Agrupa como en la pagina: un dia → sus ventanas reales."""
    order: list[str] = []
    groups: dict[str, list[dict]] = {}
    for o in options:
        d = o.get("date") or ""
        if not d:
            continue
        if d not in groups:
            groups[d] = []
            order.append(d)
        groups[d].append(o)
    return [(d, groups[d]) for d in order]


def _format_availability_live(options: list[dict], limit_days: int = 10) -> str:
    """
    Formato tipo pagina Safelite: dias con sus ventanas reales del API.
    Cada linea lleva el DIA claro (para envio a domicilio).
    Nada inventado — solo lo que devolvio mobile-time-slots.
    """
    # Se numeran las ventanas mas proximas en vez de listar dias agrupados:
    # el cliente responde con un numero y se cierra la cita en un solo turno.
    limite = max(1, min(SLOT_MAX_OPCIONES, limit_days * 2 if limit_days < 4 else SLOT_MAX_OPCIONES))
    texto, _usadas = _lista_numerada(options, limit=limite)
    return texto


def _first_slot_date(options: list[dict]) -> date | None:
    """Primera fecha ISO en la lista de slots (orden natural)."""
    dates = []
    for o in options or []:
        d = (o.get("date") or "").strip()[:10]
        if not d:
            continue
        try:
            dates.append(date.fromisoformat(d))
        except Exception:
            continue
    return min(dates) if dates else None



def shop_first_date(lead: dict, veh: dict | None = None) -> date | None:
    """Primera fecha de taller (InShop)."""
    opts = safelite_shop_slots(lead, veh=veh, days=14)
    return _first_slot_date(opts)


def should_offer_taller(mobile_first: date | None, shop_first: date | None) -> tuple[bool, int]:
    """
    Ofrece taller (opcion) si llega 1 o MAS dias ANTES que domicilio.
    return (offer, days_earlier)
    """
    if not mobile_first or not shop_first:
        return False, 0
    delta = (mobile_first - shop_first).days
    if delta >= 1:
        return True, delta
    return False, max(0, delta)


def _taller_offer_for_message(lead: dict, mobile_options: list[dict]) -> dict:
    """Taller opcional si shop es >=1 dia antes que mobile."""
    mobile_first = _first_slot_date(mobile_options)
    shop_first = None
    try:
        shop_first = shop_first_date(lead)
    except Exception:
        log.exception("shop_first_date in pitch")
    offer, days_earlier = should_offer_taller(mobile_first, shop_first)
    label = _fmt_day(shop_first.isoformat()) if (offer and shop_first) else ""
    return {
        "offer_taller": offer,
        "taller_days_earlier": days_earlier,
        "taller_first_label": label,
        "mobile_first": mobile_first.isoformat() if mobile_first else "",
        "shop_first": shop_first.isoformat() if shop_first else "",
    }


def _filter_slots_for_intent(options: list[dict], text: str) -> list[dict]:
    """Si el cliente nombro un dia, solo esas ventanas LIVE de ese dia."""
    if not options or not (text or "").strip():
        return list(options or [])
    intent = _parse_when_intent(text)
    if not intent.get("has_when"):
        return list(options)
    # solo dia (sin forzar hora) — filtrar por date/weekday/relative
    if not (intent.get("date") or intent.get("weekday") is not None or intent.get("relative") in (
        "hoy", "manana", "pasado_manana",
    )):
        return list(options)
    day_filtered = [o for o in options if _slot_matches_day(o, intent)]
    if not day_filtered and intent.get("weekday") is not None and intent.get("date"):
        intent2 = dict(intent)
        intent2["date"] = None
        day_filtered = [o for o in options if _slot_matches_day(o, intent2)]
    return day_filtered if day_filtered else []


def _nearest_day_slots(options: list[dict], max_days: int = 2) -> list[dict]:
    """Horas del dia(s) mas cercano(s) a domicilio — no spamear 3 semanas."""
    if not options:
        return []
    groups = _group_slots_by_day(options)[: max(1, max_days)]
    out: list[dict] = []
    for _iso, slots in groups:
        out.extend(slots)
    return out


def slots_message(
    options: list[dict],
    service_mode: str = "domicilio",
    offer_taller: bool = False,
    taller_days_earlier: int = 0,
    taller_first_label: str = "",
    user_text: str = "",
) -> str:
    """
    Disponibilidad LIVE por dia (ventanas reales API).
    Cierra con handoff: el humano confirma dia/hora.
    """
    if not options:
        if service_mode == "sucursal":
            return "Ahora no hay hueco en taller. En un momento te atiende alguien de aquí."
        return "Ahora no hay hueco a domicilio. En un momento te atiende alguien de aquí."

    intent = _parse_when_intent(user_text or "")
    day_named = bool(
        intent.get("has_when")
        and (
            intent.get("date")
            or intent.get("weekday") is not None
            or intent.get("relative") in ("hoy", "manana", "pasado_manana")
        )
    )

    if day_named:
        day_opts = _filter_slots_for_intent(options, user_text or "")
        if day_opts:
            dia = _fmt_day(day_opts[0]["date"])
            body = _list_slots_elegant(day_opts, limit=SLOT_MAX_OPCIONES)
            msg = f"Horarios en que podemos ir a tu lugar el {dia}:\n{body}"
            if offer_taller and taller_first_label and taller_days_earlier >= 1 and service_mode != "sucursal":
                msg += (
                    f"\n(En taller hay {taller_days_earlier} día(s) antes: {taller_first_label}.)"
                )
            msg += "\n\n¿Cuál te sirve? Dime el día que te acomode (o el número)."
            return msg
        # pidio un dia sin hueco mobile
        dia = _human_intent_day(intent)
        nearest = _nearest_day_slots(options, max_days=2)
        body = _format_availability_live(nearest, limit_days=2) if nearest else _format_availability_live(options, limit_days=2)
        msg = (
            f"Ese día no podemos ir a tu lugar. "
            f"Días con envío a domicilio:\n{body}"
        )
        if offer_taller and taller_first_label and taller_days_earlier >= 1:
            msg += f"\n(En taller hay antes: {taller_first_label}.)"
        msg += "\n\n¿Cuál te sirve? Dime el día que te acomode (o el número)."
        return msg

    # domicilio default: lo mas cercano — siempre con el DIA visible
    # Cierre: horarios + pregunta de dia (humano cierra calle/tel)
    if service_mode == "sucursal":
        body = _format_availability_live(options, limit_days=4)
        return (
            f"Horarios en taller:\n{body}\n\n"
            f"¿Cuál te sirve? Dime el día que te acomode (o el número)."
        )

    nearest = _nearest_day_slots(options, max_days=3)
    body = _format_availability_live(nearest or options, limit_days=3)
    first = _first_slot_date(nearest or options)
    first_txt = _fmt_day(first.isoformat()) if first else ""

    if first_txt:
        msg = (
            f"Horarios en que podemos ir a tu lugar — lo más pronto es el {first_txt}:\n"
            f"{body}"
        )
    else:
        msg = (
            f"Horarios en que podemos ir a tu lugar:\n"
            f"{body}"
        )

    if offer_taller and taller_first_label and taller_days_earlier >= 1:
        msg += (
            f"\n(En taller hay {taller_days_earlier} día(s) antes: {taller_first_label}.)"
        )
    msg += "\n\n¿Cuál te sirve? Dime el número de la lista y te busco ese día."
    return msg


def _msg_day_unavailable(intent: dict, alts: list[dict], lead: dict | None = None, user_text: str = "") -> str:
    """
    Si pide HOY/MAÑANA y no hay domicilio ese dia:
    - no spamear sabado domicilio a ciegas
    - ofrecer TALLER (si hay) — aprendizaje Angel Oliva
    """
    dia = _human_intent_day(intent)
    rel = intent.get("relative") or ""
    lead = lead or {}

    # Urgencia hoy o manana sin mobile → taller primero
    if rel in ("hoy", "manana", "pasado_manana") or _wants_urgency_today(user_text):
        shop_opts: list[dict] = []
        try:
            if _slots_ready(lead):
                shop_opts = safelite_shop_slots(lead, days=10)
        except Exception:
            log.exception("shop on day_unavailable")
        if shop_opts:
            # filtrar al dia pedido si es manana/pasado
            day_shop = [o for o in shop_opts if _slot_matches_day(o, intent)] if intent.get("date") else []
            use = day_shop or shop_opts
            # resumen por dia (pocas lineas)
            body = _format_availability_live(use, limit_days=2)
            first = _first_slot_date(use)
            first_txt = _fmt_day(first.isoformat()) if first else ""
            if rel == "hoy":
                # hoy mobile no hay: honesto + taller lo mas pronto
                msg = f"Hoy envío a domicilio no se puede."
                if first_txt:
                    msg += f" Si lo llevas al taller, lo más pronto es el {first_txt}:\n{body}"
                else:
                    msg += f" En taller tengo:\n{body}"
                msg += "\n¿Te sirve taller (tú lo llevas)?"
                return msg
            # mañana sin mobile: es taller
            msg = f"Mañana envío a domicilio no hay."
            if day_shop:
                msg += f" En taller mañana sí:\n{body}\n¿Qué hora te acomoda en taller?"
            else:
                msg += f" En taller lo más pronto es el {first_txt}:\n{body}\n¿Te sirve taller?"
            return msg

    if not alts:
        return f"{dia.capitalize()} ya no hay espacio. ¿Probamos otra fecha?"
    return (
        f"{dia.capitalize()} a domicilio no hay. "
        f"Horarios en que podemos ir a tu lugar, lo más pronto:\n{_list_slots_elegant(alts, 4)}\n¿Cuál te sirve?"
    )


def _msg_time_unavailable(intent: dict, day_slots: list[dict]) -> str:
    dia = _human_intent_day(intent)
    periodo = intent.get("period")
    hint = {"manana": "en la mañana", "tarde": "en la tarde", "noche": "en la noche"}.get(
        periodo or "", "a esa hora"
    )
    if not day_slots:
        return f"Ese horario {hint} el {dia} no está libre. ¿Otro día?"
    return f"Esa hora no, pero el {dia} tengo:\n{_list_slots_elegant(day_slots, 3)}\n¿Te sirve alguno?"


def _msg_clarify_day(iso_date: str, day_slots: list[dict]) -> str:
    return f"El {_fmt_day(iso_date)} tengo:\n{_list_slots_elegant(day_slots, 3)}\n¿Mañana o tarde?"


def parse_slot_choice(text: str, options: list[dict]) -> dict | None:
    """Compat: devuelve slot si el texto matchea; si no None."""
    r = interpret_slot_request(text, options)
    if r["status"] == "matched":
        return r["slot"]
    return None


def _resolve_vehicle(psid: str, lead: dict) -> dict | None:
    """
    Resuelve el vehiculo contra Safelite tolerando errores de tipeo del cliente.

    1) Intenta con el texto crudo tal cual lo escribio el cliente.
    2) Si falla, normaliza make/model contra el catalogo real de Safelite
       (app/vehicle_norm) y reintenta con la grafia correcta.
    3) Si la version normalizada funciona, persiste el make/model corregido
       en el lead para que el resto del flujo (recap, booking) use el bueno.

    Devuelve el dict de safelite_vehicle o None. La desambiguacion cuando la
    normalizacion queda 'ambiguous' es capa 2 (otro PR): aca solo se registra.
    """
    year, make, model = lead.get("year"), lead.get("make"), lead.get("model")
    if _is_junk_model(model, make):
        salvaged = _salvage_model_from_chat(psid, lead)
        if salvaged:
            model = salvaged
            lead["model"] = salvaged
            upsert_lead(psid, model=salvaged)
    # King Ranch / Super Duty / "King" suelto nunca es un modelo Safelite.
    blob = f"{make or ''} {model or ''}".lower()
    if re.search(r"\bking(\s*ranch)?\b|\bsuper\s*duty\b", blob):
        make = make or "Ford"
        model = "F Series F250"

    hint = str(lead.get("style") or "")
    veh = safelite_vehicle(year, make, model, style_hint=hint)
    if veh:
        return veh

    try:
        norm = normalize_vehicle(year, make, model)
    except Exception:
        log.exception("normalize_vehicle %s %s %s", year, make, model)
        return None

    n_make, n_model = norm.get("make"), norm.get("model")
    if not (n_make and n_model):
        # con candidatos reales del catalogo se puede preguntar en vez de
        # mandar al humano (capa de desambiguacion)
        if (norm.get("confidence") == "ambiguous"
                and len(norm.get("candidates") or []) >= 2):
            lead["_veh_candidates"] = norm["candidates"]
            lead["_veh_make"] = n_make or make
            lead["_veh_confidence"] = "ambiguous"
        log.info(
            "vehicle_norm sin resolver psid=%s %s %s %s conf=%s candidatos=%s",
            psid, year, make, model, norm.get("confidence"), norm.get("candidates"),
        )
        return None
    if (n_make, n_model) == (make, model):
        return None

    veh = safelite_vehicle(year, n_make, n_model, style_hint=hint)
    if not veh:
        return None

    log.info(
        "vehicle_norm corrigio psid=%s '%s %s' -> '%s %s' (conf=%s)",
        psid, make, model, n_make, n_model, norm.get("confidence"),
    )
    upsert_lead(psid, make=n_make, model=n_model)
    lead["make"], lead["model"] = n_make, n_model
    try:
        from .aprender import rec, remember_alias
        remember_alias(n_make or make or "", str(model or ""), n_model, psid=psid)
        rec("VEHICLE_OK", psid, f"{year} {n_make} {n_model} (era {model})", lead)
    except Exception:
        pass
    return veh


def _pregunta_modelo(lead: dict) -> str | None:
    """
    Pregunta numerada SOLO con variantes reales de lo que dijo el cliente
    (Silverado C1500/C2500, CX-7/CX-9). Si no pegamos (confidence none)
    no inventamos Accord/Civic/City porque van primero en el catalogo.
    Vale para cualquier marca, no solo Honda.
    """
    cands = lead.get("_veh_candidates")
    marca = lead.get("_veh_make") or lead.get("make")
    conf = lead.get("_veh_confidence")
    if not cands:
        try:
            norm = normalize_vehicle(lead.get("year"), lead.get("make"), lead.get("model"))
        except Exception:
            log.exception("pregunta_modelo normalize")
            return None
        conf = norm.get("confidence")
        cands = norm.get("candidates") or []
        marca = norm.get("make") or marca
    if conf and conf != "ambiguous":
        return None
    return _veh_pregunta(marca or "auto", cands)


def _is_handoff_msg(msg: str | None) -> bool:
    return bool(re.search(r"(?i)dame un momento", msg or ""))


def _ask_better_vehicle(psid: str, lead: dict) -> str:
    """
    El catalogo no trago el modelo (Ram Long, Lone Star, etc.).
    NUNCA handoff: pregunta variantes reales o pide el auto otra vez.
    """
    upsert_lead(psid, stage="calificando", bot_muted="", slots_error="esperando_modelo")
    try:
        from .aprender import rec
        rec("VEHICLE_FAIL", psid, "", lead)
    except Exception:
        pass
    q = _pregunta_modelo(lead)
    if q:
        return q
    year = str(lead.get("year") or "").strip()
    make = (lead.get("make") or "").strip()
    model = (lead.get("model") or "").strip()
    mods: list[str] = []
    if year and make:
        try:
            from .vehicle_norm import safelite_models
            mods = safelite_models(year, make) or []
        except Exception:
            log.exception("ask_better_vehicle catalog")
            mods = []
    pickups = [m for m in mods if re.search(r"1500|2500|3500|pickup|f series", m, re.I)]
    said = (model or "").strip()
    close: list[str] = []
    if said and mods:
        try:
            import difflib
            from .vehicle_norm import key as vkey, strip_glued_year
            probe = vkey(strip_glued_year(said) or said)
            by_key = {}
            for m in mods:
                by_key.setdefault(vkey(m), m)
            close = [
                by_key[c]
                for c in difflib.get_close_matches(probe, list(by_key.keys()), n=5, cutoff=0.4)
            ]
        except Exception:
            close = []
    # NUNCA los primeros 5 del catalogo en orden alfabetico (cualquiera:
    # Accord/Civic cuando dijo otra cosa). Si no hay parecidos, pedimos escrito.
    if 2 <= len(close) <= 8:
        cands = close
    elif 2 <= len(pickups) <= 8:
        cands = pickups
    else:
        cands = []
    if len(cands) >= 2:
        lead["_veh_candidates"] = cands
        lead["_veh_make"] = make or "auto"
        q = _veh_pregunta(make or "auto", cands)
        if q:
            return q
    if make and year:
        if _is_junk_model(model, make):
            salvaged = _salvage_model_from_chat(psid, lead)
            if salvaged:
                upsert_lead(psid, model=salvaged, slots_error="")
                lead["model"] = salvaged
                return (
                    f"Ok, {make} {salvaged} {year}. "
                    + (ASK_ZIP_ES if not _lead_zip(lead) else "Dame un segundo, te busco horarios.")
                )
        if re.search(r"(?i)ram|dodge|chevy|chevrolet|gmc|ford", make):
            ej = f"{make} 1500"
        elif re.search(r"(?i)mercedes", make):
            ej = f"{make} E350"
        else:
            ej = f"{year} {make} y el modelo"
        dicho = f"«{model}»" if model and not _is_junk_model(model, make) else "ese modelo"
        return (
            f"No me aparece {dicho} como {make} {year}. "
            f"Escríbeme el modelo exacto (ej: {ej}) y te paso horarios."
        )
    return (
        "Para pasarte horarios necesito año, marca y modelo otra vez. "
        "Ej: 2016 Ram 1500."
    )


def _sin_horarios_reply(psid: str, lead: dict, slots_err: str | None) -> str:
    """
    Sin horarios: preguntar, no silenciar.

    Barni Varela: "Ram long stat" se guardo como Ram Long → Safelite fail →
    'dame un momento'. Eso no se vuelve a repetir con nadie.
    """
    err = slots_err or ""
    if err in ("vehicle_ambiguo", "vehicle_fail", "vehicle_api_error", "sin_auto"):
        return _ask_better_vehicle(psid, lead)
    if err == "sin_zip":
        upsert_lead(psid, stage="calificando", bot_muted="")
        return ASK_ZIP_ES
    if err == "zip_api_error":
        upsert_lead(psid, stage="calificando", bot_muted="")
        z = _lead_zip(lead)
        prefix = f"Ya tengo el código postal {z}. " if z else ""
        return (
            f"{prefix}Aún no pude sacar horarios con ese zip. "
            "Envíeme su número de teléfono para coordinar la cita."
        )
    if err == "zip_no_servicio":
        upsert_lead(psid, stage="calificando", bot_muted="")
        return (
            "Ese código postal no tiene servicio a domicilio. "
            "¿Tienes otro zip o lo llevas al taller?"
        )
    # Parts/slots fallaron pero el auto YA está: no decir que el modelo no existe.
    if err in ("parts_fail", "parts_api_error", "slots_api_error", "sin_ventanas"):
        upsert_lead(psid, stage="calificando", bot_muted="")
        return (
            "Aún no pude sacar horarios con esos datos. "
            "Envíeme su número de teléfono para coordinar la cita."
        )
    # Modelo mal leído: preguntar, NUNCA mute.
    if lead.get("year") and lead.get("make"):
        md = (lead.get("model") or "").strip()
        if md and not _is_junk_model(md, lead.get("make")):
            try:
                from .vehicle_norm import safelite_models, key as vkey
                mods = safelite_models(lead.get("year"), lead.get("make")) or []
                if any(vkey(md) == vkey(m) or vkey(md) in vkey(m) for m in mods):
                    return (
                        "Aún no pude sacar horarios. "
                        "Envíeme su número de teléfono para coordinar la cita."
                    )
            except Exception:
                pass
        return _ask_better_vehicle(psid, lead)
    upsert_lead(psid, stage="calificando", bot_muted="")
    return (
        "Aún no pude sacar horarios. Escríbeme año, marca, modelo y zip. "
        "Ej: 2016 Ram 1500 75216."
    )


def _pregunta_modelo_reciente(lead: dict | None) -> list[str]:
    """Busca en los últimos mensajes del bot la lista numerada de modelos."""
    for t in reversed(_hist_texts(lead, "assistant")[-8:]):
        vistos = _veh_candidatos_mostrados(t)
        if len(vistos) >= 2:
            return vistos
    return []


def _already_asked_model_pick(lead: dict | None) -> bool:
    """Ya le mandamos '¿Cuál de estos es tu X?' a este cliente (cualquier marca)."""
    return len(_pregunta_modelo_reciente(lead)) >= 2


def _try_resolve_stored_model(psid: str, lead: dict) -> bool:
    """
    Si el texto que ya tenemos ahora pega en el catalogo (typo, año pegado),
    lo corrige y sigue. Cualquier persona, no un modelo suelto.
    """
    year, make, model = lead.get("year"), lead.get("make"), lead.get("model")
    if not (year and make and model):
        return False
    try:
        norm = normalize_vehicle(year, make, model)
    except Exception:
        log.exception("try_resolve_stored_model")
        return False
    n_make, n_model = norm.get("make"), norm.get("model")
    if not (n_model and norm.get("confidence") in ("exact", "fuzzy")):
        return False
    if (n_make or make) != make or n_model != model:
        upsert_lead(
            psid,
            make=n_make or make,
            model=n_model,
            slots_error="",
            slots_json="",
            slots_fetched_at="",
            bot_muted="",
            stage="calificando",
        )
        lead["make"] = n_make or make
        lead["model"] = n_model
        try:
            from .aprender import rec, remember_alias
            remember_alias(n_make or make or "", str(model or ""), n_model, psid=psid)
            rec("VEHICLE_OK", psid, f"{year} {n_make or make} {n_model} (era {model})", lead)
        except Exception:
            pass
    return True


def _capture_model_choice(psid: str, lead: dict, text: str) -> bool:
    """
    Si el bot pregunto por la variante del modelo y el cliente contesto
    ("2", "c2500", "cx 9"), persiste el modelo elegido. Devuelve True si
    se guardo algo.
    """
    if not text:
        return False
    vistos = _pregunta_modelo_reciente(lead)
    if len(vistos) < 2:
        return False
    # Ya eligió uno de esos: un "3" después es horario, no otro auto.
    if (lead.get("model") or "") in vistos:
        return False
    elegido = _veh_parse_respuesta(text, vistos)
    if not elegido:
        # Reescribió el modelo (no un número) o la lista no tenía su auto.
        # Releer contra el catalogo COMPLETO de esa marca, no contra los 5.
        veh = extract_vehicle_from_text(text) or {}
        said = (veh.get("model") or "").strip()
        if not said:
            blob = re.sub(r"(?i)\[el cliente envio foto[^\]]*\]", "", text or "").strip()
            blob = re.sub(r"(?:19|20)\d{2}", " ", blob).strip()
            if blob and not re.fullmatch(r"(?i)no|ninguno|ninguna|ningun|no esta|no está", blob):
                said = blob
        if said:
            try:
                norm = normalize_vehicle(
                    lead.get("year") or veh.get("year"),
                    lead.get("make") or veh.get("make"),
                    said,
                )
            except Exception:
                norm = {}
            if (norm.get("model")
                    and norm.get("confidence") in ("exact", "fuzzy")):
                elegido = norm["model"]
    if not elegido:
        return False
    marca = lead.get("make")
    try:
        norm = normalize_vehicle(lead.get("year"), marca, elegido)
        marca = norm.get("make") or marca
    except Exception:
        log.exception("capture_model_choice normalize")
    log.info("modelo desambiguado psid=%s -> %s %s", psid, marca, elegido)
    old = lead.get("model") or ""
    upsert_lead(
        psid,
        make=marca,
        model=elegido,
        slots_error="",
        slots_json="",
        slots_fetched_at="",
        bot_muted="",
        stage="calificando",
    )
    lead["make"], lead["model"] = marca, elegido
    try:
        from .aprender import rec, remember_alias
        remember_alias(marca or "", old, elegido, psid=psid)
        rec("MODELO_ELEGIDO", psid, f"{marca} {elegido} (era {old})", lead)
    except Exception:
        pass
    return True


def ensure_slots_for_lead(psid: str, lead: dict, force: bool = True) -> tuple[list[dict], str | None]:
    """
    Consulta la API real de Safelite (mobile-time-slots).
    Con AUTO + ZIP basta (no hace falta calle verificada para VER horarios).
    Devuelve (options, error_code).
    error_code != None → fallo tecnico / no se pudo armar disponibilidad → handoff humano.
    NUNCA inventa horarios.
    """
    if not _has_vehicle(lead):
        log.info("ensure_slots: sin auto psid=%s", psid)
        upsert_lead(psid, slots_error="sin_auto")
        return [], "sin_auto"

    # cache solo si es fresca (< 12 min) y no forzada — los ids de ruta cambian
    if not force and lead.get("slots_json") and lead.get("slots_fetched_at"):
        try:
            age = (datetime.utcnow() - datetime.fromisoformat(lead["slots_fetched_at"])).total_seconds()
            if age < 12 * 60:
                cached = json.loads(lead["slots_json"])
                if cached:
                    upsert_lead(psid, slots_error="")
                    return cached, None
        except Exception:
            pass

    zipcode = _lead_zip(lead)
    if not zipcode:
        log.warning("ensure_slots: sin zip psid=%s", psid)
        upsert_lead(psid, slots_error="sin_zip")
        return [], "sin_zip"

    try:
        zinfo = safelite_zip(zipcode)
    except Exception:
        log.exception("ensure_slots zip api")
        upsert_lead(psid, slots_error="zip_api_error")
        return [], "zip_api_error"

    if zinfo and zinfo.get("isServiceable") is False:
        log.info("ensure_slots: zip %s no serviceable mobile", zipcode)
        upsert_lead(
            psid,
            slots_json="[]",
            slots_fetched_at=datetime.utcnow().isoformat(),
            slots_error="zip_no_servicio",
        )
        return [], "zip_no_servicio"

    try:
        veh = _resolve_vehicle(psid, lead)
    except Exception:
        log.exception("ensure_slots vehicle")
        upsert_lead(psid, slots_error="vehicle_api_error")
        return [], "vehicle_api_error"
    if not veh:
        salvaged = _salvage_model_from_chat(psid, lead)
        if salvaged and salvaged != (lead.get("model") or ""):
            upsert_lead(psid, model=salvaged)
            lead["model"] = salvaged
            veh = _resolve_vehicle(psid, lead)
    if not veh:
        if lead.get("_veh_candidates"):
            log.info("ensure_slots: vehiculo ambiguo psid=%s candidatos=%s",
                     psid, lead["_veh_candidates"])
            upsert_lead(psid, slots_error="vehicle_ambiguo")
            try:
                from .memory import client_event
                client_event(psid, "vehicle_ambiguo", f"{lead.get('year')} {lead.get('make')} {lead.get('model')}", worked=False)
            except Exception:
                pass
            return [], "vehicle_ambiguo"
        log.warning("ensure_slots: vehicle fail %s %s %s", lead.get("year"), lead.get("make"), lead.get("model"))
        upsert_lead(psid, slots_error="vehicle_fail")
        try:
            from .memory import client_event
            client_event(psid, "vehicle_fail", f"{lead.get('year')} {lead.get('make')} {lead.get('model')}", worked=False)
        except Exception:
            pass
        try:
            from .aprender import rec
            rec("VEHICLE_FAIL", psid, "", lead)
        except Exception:
            pass
        return [], "vehicle_fail"

    try:
        parts = safelite_parts(veh["carId"], zipcode)
    except Exception:
        log.exception("ensure_slots parts")
        upsert_lead(psid, slots_error="parts_api_error")
        return [], "parts_api_error"
    if not parts:
        log.warning("ensure_slots: parts fail car=%s zip=%s", veh.get("carId"), zipcode)
        upsert_lead(psid, slots_error="parts_fail")
        return [], "parts_fail"

    # LIVE desde la misma API que la pagina (mobile-time-slots)
    try:
        days = safelite_slots(veh["carId"], zipcode, veh, parts, days=21)
    except Exception:
        log.exception("ensure_slots slots api")
        upsert_lead(psid, slots_error="slots_api_error")
        return [], "slots_api_error"

    options = flatten_slots(days, limit=40)
    now = datetime.utcnow().isoformat()
    err = "" if options else "sin_ventanas"
    upsert_lead(
        psid,
        slots_json=json.dumps(options, ensure_ascii=False),
        slots_fetched_at=now,
        city=lead.get("city") or (zinfo or {}).get("city") or "",
        state=lead.get("state") or (zinfo or {}).get("state") or "",
        zip=zipcode,
        slots_error=err,
    )
    note(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] LIVE slots zip={zipcode} "
        f"auto={veh.get('year')} {veh.get('make')} {veh.get('_model') or veh.get('model')} "
        f"ventanas={len(options)} dias={len({o['date'] for o in options})} "
        f"dir={lead.get('address') or '-'} err={err or '-'}"
    )
    if not options:
        return [], "sin_ventanas"
    try:
        from .aprender import rec
        rec("SLOTS", psid, f"{len(options)} ventanas zip={zipcode}", lead)
    except Exception:
        pass
    return options, None


def client_recap(lead: dict, veh: dict, slot_date: str, st: str, et: str, precio: int) -> str:
    """Recap natural y corto. SIN portal, SIN links, SIN codigos."""
    nombre = (lead.get("name") or "").split()[0] or ""
    auto = f"{veh.get('year')} {veh.get('make')} {veh.get('_model') or veh.get('model')}"
    dir_txt = (lead.get("address") or "").strip()
    city = (lead.get("city") or "").strip()
    state = (lead.get("state") or "").strip()
    zipc = (lead.get("zip") or "").strip()
    if city or state or zipc:
        dir_full = ", ".join(x for x in [dir_txt, f"{city} {state}".strip(), zipc] if x)
    else:
        dir_full = dir_txt
    tel = lead.get("client_phone") or lead.get("phone") or ""
    mode = (lead.get("service_mode") or "domicilio").lower()
    lugar = "en sucursal" if mode == "sucursal" else "a domicilio"
    quien = f"{nombre}, " if nombre else ""
    tel_bit = f" Te escribo al {tel}." if tel else ""
    return (
        f"Listo {quien}te anoté {lugar}.\n"
        f"{auto} — {dir_full}\n"
        f"Una persona del equipo te confirma por este chat.{tel_bit}"
    )


def _wants_reschedule(text: str) -> bool:
    t = _norm_es(text or "")
    return bool(
        re.search(
            r"\b(cambiar|mover|reagendar|reprogram|otro dia|otra hora|otra fecha|"
            r"no puedo (ese|ese dia|ir)|se me complico|mejor otro|modificar)\b",
            t,
        )
    )


def _wants_cancel(text: str) -> bool:
    t = _norm_es(text or "")
    return bool(
        re.search(
            r"\b(cancelar|anular|eliminar|borrar|ya no (quiero|la quiero|puedo)|"
            r"sac(a|ar) la cita|no la quiero)\b",
            t,
        )
    )


def _confirms_rebook(text: str) -> bool:
    t = _norm_es(text or "")
    return bool(
        re.search(
            r"\b(si|sí|dale|ok|okay|va|hacelo|hazlo|haz una nueva|nueva cita|"
            r"reagend|reprogram|cancel(a|ar) y|borra y|elimina y)\b",
            t,
        )
    )


def reset_appointment(psid: str, lead: dict, reason: str = "cliente pidio reprogramar") -> None:
    """Cancela localmente la cita para armar una nueva. Libera pool. Admin ve el WO viejo en citas.txt."""
    wo = lead.get("work_order") or ""
    portal = lead.get("portal") or ""
    note("")
    note("=" * 50)
    note(f"CANCEL/REBOOK  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    note(f"Motivo  : {reason}")
    note(f"Cliente : {lead.get('name')} | {lead.get('client_phone') or lead.get('phone')}")
    note(f"WO viejo: {wo}")
    note(f"LINK    : {portal}")
    note(f"PSID    : {psid}")
    note("ACCION ADMIN: cancelar WO viejo en portal si sigue activo")
    note("=" * 50)
    free_pool_for_lead(psid, wo or None)
    upsert_lead(
        psid,
        work_order="",
        portal="",
        slot_id="",
        slot_date="",
        slot_time="",
        slots_json="",
        pool_phone="",
        pool_email="",
        stage="cerrando",
    )


def handle_post_booking(psid: str, lead: dict, text: str) -> str:
    """
    Cliente ya tiene cita: comodidad total.
    - Preguntas → recap amable
    - Cambiar: no se edita a medias (ruta armada); se cancela y se arma nueva
    - Cancelar: confirma y libera
    """
    nombre = (lead.get("name") or "").split()[0]
    pref = f"{nombre}, " if nombre else ""
    when = ""
    if lead.get("slot_date"):
        st, et = (lead.get("slot_time") or "12:00-17:00").split("-", 1)
        when = f" el {_fmt_day(lead['slot_date'])}, {_fmt_hour(st)} – {_fmt_hour(et)}"
    mode = "a domicilio" if (lead.get("service_mode") or "domicilio") != "sucursal" else "en sucursal"

    # Confirmacion de rebook/cancel tras haber ofrecido
    hist = history(psid)
    last_bot = ""
    for m in reversed(hist[-6:]):
        if m.get("role") == "assistant":
            last_bot = m.get("content") or ""
            break
    offered = bool(
        re.search(r"cancelar esta y armar|armar una nueva|dejamos sin efecto", last_bot, re.I)
    )

    if offered and _confirms_rebook(text):
        reset_appointment(psid, lead, reason=text[:120])
        lead2 = get_lead(psid)
        # ofrecer de nuevo disponibilidad LIVE
        try:
            options, slots_err = ensure_slots_for_lead(psid, lead2, force=True)
        except Exception:
            options, slots_err = [], "exception"
        if options:
            pitch = _taller_offer_for_message(lead2, options)
            return (
                f"{pref}listo, cita nueva:\n"
                + slots_message(
                    options,
                    "domicilio",
                    offer_taller=pitch["offer_taller"],
                    taller_days_earlier=pitch["taller_days_earlier"],
                    taller_first_label=pitch["taller_first_label"],
                    user_text=text or "",
                )
            )
        # sin disponibilidad: preguntar auto/zip, no silenciar
        return _sin_horarios_reply(psid, lead2, slots_err)

    if _wants_cancel(text) and not _wants_reschedule(text):
        return f"{pref}sin problema. ¿Cancelamos la visita{when}?"

    if _wants_reschedule(text) or _wants_cancel(text):
        return f"{pref}ok, ¿cancelo esta{when} y te busco otra fecha?"

    # pregunta general post-cita
    if re.search(r"\b(cuando|a que hora|que dia|direccion|donde|precio|cuanto)\b", _norm_es(text or "")):
        precio = quote_price(
            lead.get("year"), lead.get("make") or "", lead.get("model") or "",
            lead=lead, psid=lead.get("psid"),
        )
        return f"{pref}tu cita sigue confirmada{when} {mode}, {_fmt_price_num(precio)} con garantia."

    return f"{pref}tu cita ya está confirmada{when} {mode}. Cualquier cosa me escribes."


def try_book(psid: str, lead: dict, want_date: str = "") -> str | None:
    """
    Agenda REAL en Safelite (equivalente a Continue en
    https://www.safelite.com/fmg/schedule):
      save-session/fmg → load-session → submit-order (submitAfterSave=True)

    Solo llamar DESPUES de que el cliente confirmo (confirm_pending → SI).
    Pool nm/email para la web; el cliente no ve Safelite ni links.
    """
    if lead.get("work_order"):
        return None
    if not _ready(lead):
        return None
    if not lead.get("slot_id") or not lead.get("slot_date"):
        return None

    zipcode = lead.get("zip") or _extract_zip(lead.get("address") or "")
    if not zipcode:
        return "Me falta el código postal."

    z = safelite_zip(zipcode)
    if not z or not z.get("isServiceable"):
        return "Ese zip no cubre. ¿Otro zip?"

    if not lead.get("city"):
        upsert_lead(psid, city=z.get("city"), state=z.get("state"), zip=zipcode)
        lead = get_lead(psid)

    veh = safelite_vehicle(
        lead["year"], lead["make"], lead["model"], style_hint=str(lead.get("style") or "")
    )
    if not veh:
        return "No me cuadra el auto. ¿Año, marca y modelo?"

    parts = safelite_parts(veh["carId"], zipcode)
    if not parts:
        return "Se me trabo. Dame un momento."

    slot = {
        "id": lead["slot_id"],
        "startTime": (lead.get("slot_time") or "12:00-17:00").split("-")[0],
        "endTime": (lead.get("slot_time") or "12:00-17:00").split("-")[-1],
    }
    try:
        for o in json.loads(lead.get("slots_json") or "[]"):
            if o.get("id") == lead["slot_id"]:
                slot = {"id": o["id"], "startTime": o["startTime"], "endTime": o["endTime"]}
                break
    except Exception:
        pass

    pool_phone = lead.get("pool_phone") or assign_pool("phone", psid)
    pool_email = lead.get("pool_email") or assign_pool("email", psid)
    if not pool_phone or not pool_email:
        return "Dame 1 min y te busco ese día."
    upsert_lead(psid, pool_phone=pool_phone, pool_email=pool_email)
    lead = get_lead(psid)

    try:
        booking = safelite_book(lead, veh, parts, slot, lead["slot_date"])
    except Exception as e:
        log.exception("book fail")
        free_pool_for_lead(psid)
        upsert_lead(psid, pool_phone="", pool_email="")
        note(f"[{datetime.now().isoformat()}] ERROR booking psid={psid}: {e}")
        return "Se me corto. Dame un momento."

    portal = booking.get("portal") or ""
    wo = booking.get("work_order") or ""
    bind_pool_work_order(psid, wo)
    precio = quote_price(
        lead.get("year"), lead.get("make") or "", lead.get("model") or "",
        lead=lead, psid=psid or lead.get("psid"),
    )
    slot_date = lead["slot_date"]
    st, et = slot["startTime"], slot["endTime"]

    upsert_lead(
        psid,
        stage="ganado",
        slot_time=f"{st}-{et}",
        work_order=wo,
        portal=portal,
        zip=zipcode,
        booked_at=datetime.now().isoformat(timespec="seconds"),
        finished_at="",
        confirm_pending="",
    )

    note("")
    note("=" * 50)
    note(f"CITA REAL (Safelite submit-order / Continue)  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    note(f"Cliente : {lead.get('name')} | tel cliente {lead.get('client_phone') or lead.get('phone') or '-'}")
    note(f"Pool WEB: nm {pool_phone} | email {pool_email}")
    note(f"Auto    : {veh.get('year')} {veh.get('make')} {veh.get('_model') or veh.get('model')} ({veh.get('_style')})")
    note(f"Precio  : ${precio}")
    note(f"Cristal : Parabrisas (Windshield)")
    note(f"Dir     : {lead.get('address') or ''} {lead.get('city') or ''} {lead.get('state') or ''} {zipcode}")
    note(f"API     : save-session/fmg → load-session → submit-order (como fmg/schedule Continue)")
    modo = (lead.get("service_mode") or "domicilio").upper()
    note(f"Cuando  : {slot_date}  {st}-{et}  {modo}")
    note(f"WO      : {wo}  |  EON {booking.get('eon')}  |  {booking.get('status')}")
    note(f"LINK ADMIN: {portal}")
    note(f"PSID    : {psid}")
    note("=" * 50)
    try:
        from .safelite_acceso import remember
        remember(
            email=pool_email or lead.get("email") or "",
            phone=lead.get("client_phone") or lead.get("phone") or pool_phone or "",
            name=lead.get("name") or "",
            woid=booking.get("work_order_id") or "",
            work_order=wo,
            portal=portal,
            source="book",
        )
    except Exception:
        pass

    return client_recap(lead, veh, slot_date, st, et, precio)


def note_finalizada(lead: dict, reason: str = "admin finalizo") -> None:
    """Guarda el cliente finalizado en apartado SEPARADO (citas_finalizadas.txt)."""
    if not CITAS_DONE.exists():
        CITAS_DONE.write_text(
            "CITAS FINALIZADAS — aparte de las activas\n"
            "Cada bloque = trabajo terminado (no vuelve a la agenda del dia).\n\n",
            encoding="utf-8",
        )
    with open(CITAS_DONE, "a", encoding="utf-8") as f:
        def w(s=""):
            f.write(s + "\n")
        w("")
        w("=" * 50)
        w(f"FINALIZADA  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        w(f"Motivo  : {reason}")
        w(f"Cliente : {lead.get('name') or '-'} | tel {lead.get('client_phone') or lead.get('phone') or '-'}")
        w(f"Auto    : {lead.get('year') or ''} {lead.get('make') or ''} {lead.get('model') or ''}".strip())
        w(f"Dir     : {lead.get('address') or ''} {lead.get('city') or ''} {lead.get('state') or ''} {lead.get('zip') or ''}".strip())
        w(f"Cuando  : {lead.get('slot_date') or '-'}  {lead.get('slot_time') or '-'}  {(lead.get('service_mode') or 'domicilio').upper()}")
        w(f"WO      : {lead.get('work_order') or '-'}")
        w(f"Portal  : {lead.get('portal') or '-'}")
        w(f"PSID    : {lead.get('psid') or '-'}")
        w("=" * 50)
    note(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] FINALIZADA → citas_finalizadas.txt psid={lead.get('psid')} wo={lead.get('work_order') or '-'}")


def build_agenda() -> dict:
    """
    Tablero de citas super visible para operar el dia.
    Agrupa: hoy / manana / esta_semana / proximas / atrasadas / finalizadas.
    """
    today = date.today()
    tomorrow = today + timedelta(days=1)
    day_after = today + timedelta(days=2)
    week_end = today + timedelta(days=7)
    leads = list_leads()

    def day_bucket(iso: str) -> str:
        if not iso:
            return "sin_fecha"
        try:
            d = date.fromisoformat(str(iso)[:10])
        except Exception:
            return "sin_fecha"
        if d < today:
            return "atrasadas"
        if d == today:
            return "hoy"
        if d == tomorrow:
            return "manana"
        if d == day_after:
            return "pasado_manana"
        if d <= week_end:
            return "esta_semana"
        return "proximas"

    def card(l: dict) -> dict:
        auto = " ".join(x for x in [l.get("year") or "", l.get("make") or "", l.get("model") or ""] if x).strip()
        dir_full = " ".join(
            x for x in [
                l.get("address") or "",
                l.get("city") or "",
                l.get("state") or "",
                l.get("zip") or "",
            ]
            if x
        ).strip()
        st = (l.get("stage") or "nuevo").lower()
        finished = st in ("finalizado", "completado", "done", "finished") or bool(l.get("finished_at"))
        return {
            "psid": l.get("psid"),
            "name": l.get("name") or "Sin nombre",
            "phone": l.get("client_phone") or l.get("phone") or "",
            "auto": auto or "—",
            "address": dir_full or "—",
            "slot_date": l.get("slot_date") or "",
            "slot_time": l.get("slot_time") or "",
            "service_mode": (l.get("service_mode") or "domicilio"),
            "work_order": l.get("work_order") or "",
            "portal": l.get("portal") or "",
            "stage": st,
            "finished": finished,
            "finished_at": l.get("finished_at") or "",
            "booked_at": l.get("booked_at") or "",
            "admin_note": l.get("admin_note") or "",
            "pool_phone": l.get("pool_phone") or "",
            "pool_email": l.get("pool_email") or "",
            "day_label": _fmt_day(l.get("slot_date") or "") if l.get("slot_date") else "sin fecha",
            "bucket": day_bucket(l.get("slot_date") or ""),
        }

    # citas activas = tienen fecha o WO y NO finalizadas
    active: list[dict] = []
    finished: list[dict] = []
    for l in leads:
        has_appt = bool(l.get("slot_date") or l.get("work_order"))
        if not has_appt:
            continue
        c = card(l)
        if c["finished"]:
            finished.append(c)
        else:
            active.append(c)

    def sort_key(c):
        return (c.get("slot_date") or "9999", c.get("slot_time") or "", c.get("name") or "")

    active.sort(key=sort_key)
    finished.sort(key=lambda c: c.get("finished_at") or c.get("slot_date") or "", reverse=True)

    groups = {
        "hoy": [c for c in active if c["bucket"] == "hoy"],
        "manana": [c for c in active if c["bucket"] == "manana"],
        "pasado_manana": [c for c in active if c["bucket"] == "pasado_manana"],
        "esta_semana": [c for c in active if c["bucket"] == "esta_semana"],
        "proximas": [c for c in active if c["bucket"] == "proximas"],
        "atrasadas": [c for c in active if c["bucket"] == "atrasadas"],
        "sin_fecha": [c for c in active if c["bucket"] == "sin_fecha"],
    }

    # finalizados de hoy (por finished_at o slot_date hoy)
    fin_hoy = []
    for c in finished:
        fa = (c.get("finished_at") or "")[:10]
        if fa == today.isoformat() or (not fa and c.get("slot_date") == today.isoformat()):
            fin_hoy.append(c)

    return {
        "today": today.isoformat(),
        "tomorrow": tomorrow.isoformat(),
        "day_after": day_after.isoformat(),
        "labels": {
            "hoy": f"1  Hoy · {_fmt_day(today.isoformat())}",
            "manana": f"2  Mañana · {_fmt_day(tomorrow.isoformat())}",
            "pasado_manana": f"3  Pasado · {_fmt_day(day_after.isoformat())}",
            "esta_semana": "Esta semana",
            "proximas": "Más adelante",
            "atrasadas": "Atrasadas",
            "sin_fecha": "Sin fecha",
            "finalizadas": "Finalizadas",
        },
        "counts": {
            "hoy": len(groups["hoy"]),
            "manana": len(groups["manana"]),
            "pasado_manana": len(groups["pasado_manana"]),
            "esta_semana": len(groups["esta_semana"]) + len(groups["hoy"]) + len(groups["manana"]) + len(groups["pasado_manana"]),
            "proximas": len(groups["proximas"]),
            "atrasadas": len(groups["atrasadas"]),
            "activas": len(active),
            "finalizadas": len(finished),
            "finalizadas_hoy": len(fin_hoy),
        },
        "groups": groups,
        "finalizadas": finished[:80],
        "finalizadas_hoy": fin_hoy,
    }


_VARIANTES_REPE = (
    "Perdona si me repito. ",
    "Te resumo otra vez. ",
    "Sigue igual. ",
)
_RECAP_MUERTO = re.compile(
    r"(?i)(te resumo( otra vez)?|sigue igual|perdona si me repito|"
    r"ok[,.]?\s+\w+(\s+\w+){0,3}\s+20\d{2}\s*[.!]?\s*$)"
)


def _looks_like_car_recap(reply: str, lead: dict | None = None) -> bool:
    """True si el mensaje solo recita el auto (eso enoja y mata el lead)."""
    t = (reply or "").strip()
    if not t:
        return True
    if _asks_for_zip(t):
        return False
    if _RECAP_MUERTO.search(t):
        return True
    lead = lead or {}
    if not _has_vehicle(lead):
        return False
    label = _vehicle_label(lead).lower()
    low = t.lower()
    if label and label in low and len(t) < 90:
        return True
    mk = (lead.get("make") or "").lower()
    md = (lead.get("model") or "").lower()
    y = str(lead.get("year") or "")
    if mk and md and y and mk in low and md in low and y in low and not _asks_for_zip(t):
        if len(t) < 120:
            return True
    return False


def _norm_ask(text: str) -> str:
    t = _norm_es(text or "")
    t = re.sub(r"ok,\s+\w+(\s+\w+)*\s+\d{4}\s*[.!?—-]*\s*", "", t)
    t = re.sub(r"[¿?¡!.,;:•]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _ask_fingerprint(text: str) -> str:
    """Familia de la pregunta. Misma huella 2 veces = bucle (Jean Lafontan)."""
    low = _norm_es(text or "")
    # Horarios primero: el encabezado lleva "código postal" y el cierre pide
    # número. Si zip/tel ganan, el primer envío de slots se come (Hery Fonseca).
    if _is_slots_outbound(text) or re.search(
        r"(cu[aá]l te sirve|qu[eé] d[ií]a te acomoda|n[uú]mero o d[ií]a)",
        low,
    ):
        return "ask_slot"
    if re.search(
        r"(tel[eé]fono.*marcar|marcarte ahora|para llamarle|te marco|"
        r"llamarnos por facebook|ll[aá]manos por facebook|call us on facebook)",
        low,
    ):
        return "ask_phone"
    if re.search(r"dame un momento", low):
        return "handoff"
    if re.search(r"cu[aá]l de estos es tu|resp[oó]ndeme con el n[uú]mero de la lista", low):
        return "ask_model_pick"
    if re.search(r"qu[eé] modelo", low):
        return "ask_model"
    if re.search(r"(de qu[eé] a[nñ]o|qu[eé] a[nñ]o\b)", low) and not re.search(
        r"a[nñ]o,?\s*marca y modelo", low
    ):
        return "ask_year"
    if re.search(
        r"(qu[eé] carro|a[nñ]o,?\s*marca y modelo|marca y modelo|"
        r"cuando me digas el carro|necesito a[nñ]o)",
        low,
    ):
        return "ask_vehicle"
    if re.search(r"(c[oó]digo postal|\bzip\b|5 n[uú]meros)", low) and not re.search(
        r"cuando me digas el carro", low
    ):
        return "ask_zip"
    return "txt:" + _norm_ask(text)[:100]


def _would_repeat(lead: dict | None, reply: str) -> bool:
    """True si este reply es la 2ª vez (o más) de la misma pregunta."""
    reply = (reply or "").strip()
    if not reply:
        return False
    prevs = _hist_texts(lead, "assistant")[-6:]
    if not prevs:
        return False
    fp = _ask_fingerprint(reply)
    if not fp.startswith("txt:"):
        return any(_ask_fingerprint(p) == fp for p in prevs)
    a = _norm_ask(reply)
    if not a:
        return False
    for p in prevs[-3:]:
        b = _norm_ask(p)
        if a == b:
            return True
        if a and b and min(len(a), len(b)) >= 24 and (a in b or b in a):
            return True
    return False


def _evitar_repetir(lead: dict, reply: str) -> str:
    """
    Si ibamos a mandar lo mismo otra vez: no rotar a otra frase igual.
    El caller corta el bucle (pedir tel y mute).
    NUNCA anteponer 'te resumo / sigue igual' — eso echa clientes.
    """
    if not reply:
        return reply
    if _has_vehicle(lead) and not _lead_zip(lead):
        if _looks_like_car_recap(reply, lead) or not _asks_for_zip(reply):
            if _already_asked(lead, "zip"):
                return _fb_call_line(lead)
            return _ask_zip_now(lead)
        return reply
    return reply


def _scrub_client_reply(reply: str, lead: dict | None = None) -> str:
    """Cinturon: nunca links ni Safelite; precio siempre 220$; no re-pedir datos."""
    reply = re.sub(r"https?://\S+", "", reply or "", flags=re.I)
    reply = re.sub(r"\bSafelite\b", "", reply, flags=re.I)
    # precios: $210 / 210 USD / ~$210 → 210$
    reply = re.sub(r"~?\s*\$\s*(2\d{2}|3[0-2]\d)\s*(?:USD|usd|us\$)?", r"\1$", reply)
    reply = re.sub(r"\b(2\d{2}|3[0-2]\d)\s*USD\b", r"\1$", reply, flags=re.I)
    reply = re.sub(r"\b(2\d{2}|3[0-2]\d)\$(?=\$)", r"\1$", reply)
    # Las listas numeradas ahora son deliberadas: el cliente responde con el
    # numero y se cierra la cita en un turno. Antes se convertian en viñetas
    # ("suavizar menus") y por eso no habia forma de elegir. Solo se limpia
    # el prefijo "Opcion N:" que si suena a robot.
    reply = re.sub(r"(?im)^\s*opci[oó]n\s*(\d+)\s*[:.)-]\s*", r"\1) ", reply)
    reply = re.sub(r"(?i)\bopci[oó]n\s*(\d+)\b(?=[^)\n]*$)", r"\1", reply)
    reply = re.sub(r"[ \t]{2,}", " ", reply)
    reply = re.sub(r"\n{3,}", "\n\n", reply)
    reply = reply.strip()
    if lead is not None:
        reply = _strip_reask_known(reply, lead)
        # Auto listo y sin ZIP: el reply TIENE que pedir el postal. Nunca resumir el carro.
        if _has_vehicle(lead) and not _lead_zip(lead):
            if not _asks_for_zip(reply):
                reply = _fb_call_line(lead) if _already_asked(lead, "zip") else _ask_zip_now(lead)
        # Calle/nombre/tel del cliente nunca. Llamada por Facebook.
        if re.search(
            r"(?i)(pasas tu direcci[oó]n|dame tu direcci[oó]n|direcci[oó]n completa|"
            r"calle con n[uú]mero|calle y zip|me pasas (la |tu )?calle|"
            r"nombre y tel[eé]fono)",
            reply,
        ) or (
            not _has_slot(lead)
            and not _already_showed_slots(lead)
            and re.search(r"(?i)(tu tel[eé]fono|phone number|n[uú]mero de tel[eé]fono)", reply)
        ):
            reply = re.sub(
                r"(?i)[^.?!]*(pasas tu direcci[oó]n|dame tu direcci[oó]n|"
                r"direcci[oó]n completa|calle con n[uú]mero|calle y zip|"
                r"me pasas (la |tu )?calle|nombre y tel[eé]fono|tu tel[eé]fono|"
                r"phone number)[^.?!]*[.?!]?\s*",
                "",
                reply,
            ).strip()
            if not reply or len(reply) < 8:
                if _has_slot(lead) or _already_showed_slots(lead):
                    reply = _note_slot_for_human(lead)
                elif _slots_ready(lead):
                    reply = "¿Qué día te acomoda de los que te pasé?"
                elif _has_vehicle(lead):
                    reply = ASK_ZIP_ES
                else:
                    reply = "¿Qué carro tienes? Año, marca y modelo."
        _algo_del_auto = any(
            str(lead.get(k) or "").strip() for k in ("year", "make", "model")
        )
        if _already_asked(lead, "vehicle") and not _has_vehicle(lead) and not _algo_del_auto:
            if re.search(r"(?i)(qu[eé]\s+carro|a[nñ]o,?\s*marca y modelo)", reply):
                if not re.search(r"(?i)necesito", reply):
                    reply = "Cuando me digas el carro y tu código postal te muestro los horarios en que podemos ir a tu lugar."
        # Sin ZIP no uses "te resumo el carro": eso es el bucle que echa clientes.
        if _has_vehicle(lead) and not _lead_zip(lead):
            if not _asks_for_zip(reply) or re.search(
                r"(?i)(te resumo|sigue igual|perdona si me repito|ok,\s+\w+\s+\w+\s+20\d{2}\s*\.?$)",
                reply,
            ):
                reply = _fb_call_line(lead) if _already_asked(lead, "zip") else _ask_zip_now(lead)
        else:
            reply = _evitar_repetir(lead, reply)
    return _rewrite_phone_asks(reply, lead).strip()


def _looks_truncated(text: str) -> bool:
    """Detecta reply cortado a mitad (ej. '¿Cuál es tu call')."""
    t = (text or "").strip()
    if not t:
        return True
    # pregunta/exclamacion abierta sin cerrar
    if re.search(r"[¿¡]\s*$", t):
        return True
    # abrio ¿ o ¡ y nunca cerro con ? o !
    if t.count("¿") > t.count("?") or t.count("¡") > t.count("!"):
        return True
    # termina en conector / palabra a medias típica de corte
    if re.search(
        r"\b("
        r"call|calle|dire|direcc|direcci[oó]n?|nom|nomb|telef|tel[eé]f|"
        r"cu[aá]l|como|c[oó]mo|para|con|por|del|los|las|una|uno|tu|su|mi|"
        r"el|la|de|en|a|y|o|si|s[ií]|que|qu[eé]|me|te|le|al|es|un|"
        r"agenda|agendar|podr[ií]as|podrias|dame|pasame|pasás|pasas"
        r")\s*$",
        t,
        re.I,
    ):
        # "si" / "ok" solos al final de frase cortas pueden ser validos
        if re.fullmatch(r"(?i)(ok|si|sí|no|ya|listo|perfecto|gracias)[.!?…]*", t):
            return False
        # si la frase es larga y termina en conector → casi seguro truncado
        if len(t) > 25:
            return True
    # termina sin puntuacion y la ultima "palabra" parece muñon (3-4 letras raras)
    # (antes el regex de fin estaba roto y marcaba "…casa?" como truncado)
    if not re.search(r"[.!?…)]\s*$", t) and len(t) > 60:
        last = re.findall(r"\S+", t)
        if last:
            w = re.sub(r"[^\wáéíóúñÁÉÍÓÚÑ]", "", last[-1])
            # muñones tipicos: call, dire, nom, tel, cal, cuan…
            if w and len(w) <= 4 and w.lower() not in {
                "ok", "si", "sí", "no", "ya", "hoy", "pm", "am", "zip", "usa",
                "sc", "nm", "tx", "ca", "fl", "ny", "az", "co", "va", "nc",
                "dia", "día", "mes", "año", "auto", "suv", "ram", "casa",
            }:
                return True
    return False


def _repair_truncated_reply(reply: str, lead: dict | None = None) -> str:
    """Si el texto vino cortado, deja solo oraciones completas o un fallback seguro."""
    t = (reply or "").strip()
    # recorta hasta la ultima oracion completa
    ends = list(re.finditer(r"[.!?…](?:\s+|$)", t))
    if ends:
        base = t[: ends[-1].end()].strip()
        # si la ultima oracion completa ya es util, usala
        if len(base) >= 35 and not _looks_truncated(base):
            return base
        # si quedo corta, intenta penultima
        if len(ends) >= 2:
            base2 = t[: ends[-2].end()].strip()
            if len(base2) >= 35 and not _looks_truncated(base2):
                return base2

    lead = lead or {}
    missing = []
    if not (lead.get("year") and lead.get("make") and lead.get("model")):
        missing.append("año marca y modelo del auto")
    if not (lead.get("address") or lead.get("zip")):
        missing.append("tu direccion")
    elif _address_gaps(lead):
        g = _address_gaps(lead)[0]
        labels = {
            "calle_y_numero": "la calle con el numero",
            "zip": "el código postal",
            "ciudad": "la ciudad",
            "estado": "el estado",
        }
        missing.append(labels.get(g, "tu direccion"))
    if missing:
        return f"Se me corto. ¿{' y '.join(missing)}?"
    if not lead.get("name") or not lead.get("phone"):
        return _fb_call_line(lead)
    return "Se me corto. Mándame de nuevo."


def _ensure_complete_reply(reply: str, lead: dict | None = None, truncated_flag: bool = False) -> str:
    """Nunca mandar al cliente un mensaje a medias; siempre compacto. No re-pide datos ya sabidos."""
    reply = _scrub_client_reply(reply, lead)
    if truncated_flag or _looks_truncated(reply):
        log.warning("reply truncado detectado → repair | was=%r", (reply or "")[:140])
        fixed = _repair_truncated_reply(reply, lead)
        return _trim_habladero(_scrub_client_reply(fixed, lead))
    return _trim_habladero(reply)


def _save_slot_choice(psid: str, chosen: dict) -> None:
    upsert_lead(
        psid,
        slot_id=str(chosen["id"]),
        slot_date=chosen["date"],
        slot_time=f"{chosen['startTime']}-{chosen['endTime']}",
        stage="cerrando",
    )


def _job_details_line(lead: dict | None = None, lang: str = "es") -> str:
    """Lo que el cliente está buscando, en una línea (auto · vidrio · zip · precio)."""
    lead = _complete_vehicle_fields(lead or {})
    auto = _vehicle_label(lead)
    glass = (lead.get("glass") or "").strip()
    if not glass or glass.lower() == "windshield":
        glass = "parabrisas" if lang == "es" else "windshield"
    zipc = _lead_zip(lead)
    city = (lead.get("city") or "").strip()
    bits: list[str] = []
    if auto:
        bits.append(auto)
    bits.append(glass)
    if zipc:
        bits.append(_t(lang, f"código {zipc}", f"ZIP {zipc}"))
    elif city:
        bits.append(city)
    return " · ".join(bits)


def _fb_call_line(lead: dict | None = None, text: str = "") -> str:
    """Cierre único: recap de lo que buscan + llamarnos por Facebook. Nunca su número."""
    lang = _client_lang(lead, text, psid=(lead or {}).get("psid"))
    details = _job_details_line(lead, lang)
    call = _t(lang, FB_CALL_ES, FB_CALL_EN)
    if details:
        return _t(
            lang,
            f"Buscas {details}. {call}",
            f"You're looking for {details}. {call}",
        )
    return _t(
        lang,
        f"Cambiamos el parabrisas a domicilio. {call}",
        f"We replace the windshield at your place. {call}",
    )


def _rewrite_phone_asks(reply: str, lead: dict | None = None, text: str = "") -> str:
    """Si el bot (o el LLM) pide el número del cliente, lo cambia a llamada por Facebook."""
    raw = (reply or "").strip()
    if not raw:
        return raw
    if not _ASK_CLIENT_PHONE_RX.search(raw):
        return raw
    cleaned = _ASK_CLIENT_PHONE_RX.sub("", raw).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    line = _fb_call_line(lead, text)
    if re.search(r"(?i)llamarnos por facebook|call us on facebook", cleaned):
        return cleaned
    return f"{cleaned}\n\n{line}".strip() if cleaned else line


def _note_slot_for_human(lead: dict, text: str = "") -> str:
    """Cierre de trabajo: recap + precio + día si ya lo eligió + Facebook."""
    lang = _client_lang(lead, text, psid=(lead or {}).get("psid"))
    details = _job_details_line(lead, lang)
    call = _t(lang, FB_CALL_ES, FB_CALL_EN)
    day = (lead.get("slot_date") or "").strip()
    tm = (lead.get("slot_time") or "").strip()
    when = ""
    if day:
        try:
            when = _fmt_day(day)
        except Exception:
            when = day
        if tm:
            st, _, et = tm.partition("-")
            try:
                when = f"{when}, {_fmt_hour(st)}–{_fmt_hour(et)}"
            except Exception:
                when = f"{when}, {tm}"
    if when and details:
        return _t(
            lang,
            f"Anotado: {details}. Te busco ese día. {call}",
            f"Noted: {details}. I have you for {when}. {call}",
        )
    if details:
        return _t(
            lang,
            f"Buscas {details}. {call}",
            f"You're looking for {details}. {call}",
        )
    return call


def _ask_street_after_slot(lead: dict, text: str = "") -> str:
    """Deprecated: ya no pedimos calle. Redirige a nota para humano."""
    return _note_slot_for_human(lead, text)


def _ask_contact_after_slot(lead: dict, text: str = "") -> str:
    """Deprecated: ya no pedimos contacto. Redirige a nota para humano."""
    return _note_slot_for_human(lead, text)


def _client_confirms_continue(text: str) -> bool:
    """SI / Continue — el cliente confirma el recap (equiv. Continue en fmg/schedule)."""
    t = _norm_es(text or "")
    if not t:
        return False
    if re.search(
        r"\b(no|nop|nel|cancel|otro dia|otra hora|cambiar|espera|mejor no|ahorita no)\b",
        t,
    ):
        return False
    return bool(
        re.search(
            r"\b(si|sí|dale|ok|okay|va|de una|confirmo|confirmalo|confirmá|"
            r"confirma|listo|hacelo|hazlo|adelante|continue|continuar|"
            r"agendalo|agéndalo|dejalo|déjalo|perfecto|va que va|claro que si|"
            r"claro que sí|yes|yep|sure)\b",
            t,
        )
    ) or re.fullmatch(r"(si|sí|ok|dale|va|yes)[.! ]*", t or "")


def _client_rejects_confirm(text: str) -> bool:
    t = _norm_es(text or "")
    return bool(
        re.search(
            r"\b(no|nop|nel|cancel|otro dia|otra hora|otra fecha|cambiar|"
            r"no confirmo|espera|mejor no|ahorita no|todavia no)\b",
            t,
        )
    )


def _booking_recap_lines(lead: dict) -> str:
    """Recap estilo review de schedule (sin decir Safelite ni links)."""
    auto = f"{lead.get('make') or ''} {lead.get('model') or ''} {lead.get('year') or ''}".strip()
    dir_txt = (lead.get("address_formatted") or lead.get("address") or "").strip()
    city = (lead.get("city") or "").strip()
    state = (lead.get("state") or "").strip()
    zipc = (lead.get("zip") or "").strip()
    if city or state or zipc:
        dir_full = ", ".join(x for x in [dir_txt, f"{city} {state}".strip(), zipc] if x)
    else:
        dir_full = dir_txt
    day = _fmt_day(lead.get("slot_date") or "")
    st, et = (lead.get("slot_time") or "12:00-17:00").split("-", 1)
    ventana = f"{_fmt_hour(st)}–{_fmt_hour(et)}"
    mode = (lead.get("service_mode") or "domicilio").lower()
    lugar = "en taller" if mode == "sucursal" else "a domicilio"
    nombre = (lead.get("name") or "").strip()
    tel = (lead.get("client_phone") or lead.get("phone") or "").strip()
    lines = [
        f"• Auto: {auto}" if auto else None,
        f"• Dónde: {dir_full} ({lugar})" if dir_full else f"• Servicio: {lugar}",
        f"• Preferencia: {day}, {ventana}",
    ]
    if nombre or tel:
        lines.append(f"• Contacto: {nombre}{' · ' + tel if tel else ''}".strip())
    return "\n".join(x for x in lines if x)


def _ask_confirm_schedule(psid: str, lead: dict) -> str:
    """
    Pantalla de confirmacion (como review antes de Continue en fmg/schedule).
    Aun NO se llama submit-order. Solo al SI del cliente.
    """
    upsert_lead(psid, confirm_pending="1", stage="confirmando")
    recap = _booking_recap_lines(lead)
    return (
        f"Te anoté esta preferencia:\n{recap}\n"
        "Una persona del equipo te confirma por este chat."
    )


def _mute_for_human(psid: str, reason: str = "") -> None:
    """Silencia el bot sin cambiar el mensaje al cliente (cierre manual)."""
    note_txt = (
        f"[{datetime.now().isoformat()}] HANDOFF humano psid={psid} "
        f"reason={reason or '-'} — bot silenciado"
    )
    note(note_txt)
    prev = get_lead(psid) or {}
    admin = (prev.get("admin_note") or "").strip()
    admin2 = (admin + "\n" if admin else "") + note_txt
    prev_err = (prev.get("slots_error") or "")
    err = (reason or prev_err or "")[:200]
    if reason == "loop_break" and prev_err.startswith("horarios_"):
        err = prev_err[:200]
    st = (prev.get("stage") or "").lower()
    if st not in ("ganado", "finalizado", "perdido"):
        st = "agente"
    upsert_lead(
        psid,
        stage=st,
        bot_muted="1",
        slots_error=err,
        admin_note=admin2[:2000],
    )
    log.warning("MUTE human psid=%s reason=%s", psid, reason)
    try:
        from .aprender import rec
        rec("MUTE", psid, reason or "-", prev)
    except Exception:
        pass


def _slots_reply_and_handoff(
    psid: str,
    lead: dict,
    options: list[dict],
    mode: str = "domicilio",
    pitch: dict | None = None,
    user_text: str = "",
) -> str:
    """Muestra horarios LIVE por dia y pasa a humano (alguien te atiende)."""
    pitch = pitch or {}
    offer_t = bool(pitch.get("offer_taller") and mode != "sucursal")
    reply = slots_message(
        options,
        mode if mode == "sucursal" else "domicilio",
        offer_taller=offer_t,
        taller_days_earlier=pitch.get("taller_days_earlier") or 0,
        taller_first_label=pitch.get("taller_first_label") or "",
        user_text=user_text or "",
    )
    reply = _scrub_client_reply(reply, lead)
    # NO se silencia el bot aqui. Antes se hacia, y por eso ninguno de los 14
    # leads que vieron horarios logro elegir: contestaban "2" y no recibian
    # nada hasta que un humano abriera el chat. El handoff ocurre en cuanto
    # el cliente elige (_apply_slot_and_continue), que es justo el dato que
    # el humano necesita para cerrar.
    try:
        upsert_lead(psid, stage="eligiendo", slots_error="")
        note(
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] HORARIOS MOSTRADOS "
            f"psid={psid} zip={_lead_zip(lead)} "
            f"auto={lead.get('year')} {lead.get('make')} {lead.get('model')} "
            f"— esperando que elija"
        )
    except Exception:
        log.exception("marcar eligiendo")
    return reply


def _apply_slot_and_continue(psid: str, lead: dict, chosen: dict, user_text: str) -> str:
    """
    Cliente eligio hueco LIVE → lo anotamos y pasamos a humano.
    NO pedimos calle/tel. NO submit automatico (cierre manual).
    """
    _save_slot_choice(psid, chosen)
    upsert_lead(psid, confirm_pending="", stage="cerrando")
    lead = get_lead(psid)
    st, et = (lead.get("slot_time") or "12:00-17:00").split("-", 1)
    note(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] PREF HORARIO (manual) "
        f"psid={psid} zip={_lead_zip(lead)} auto={lead.get('year')} {lead.get('make')} {lead.get('model')} "
        f"dia={lead.get('slot_date')} {st}-{et} — humano cierra (calle/tel)"
    )
    reply = _scrub_client_reply(_note_slot_for_human(lead, user_text), lead)
    _mute_for_human(psid, reason=f"slot_elegido_manual:{lead.get('slot_date')}_{st}")
    return reply


def _execute_confirmed_book(psid: str, lead: dict) -> str:
    """Cliente dijo SI → submit-order real en Safelite (Continue)."""
    try:
        booked = try_book(psid, lead)
    except Exception as e:
        log.exception("try_book after confirm")
        note(f"[{datetime.now().isoformat()}] try_book confirm error psid={psid}: {e}")
        upsert_lead(psid, confirm_pending="")
        return handoff_to_human(psid, reason=f"try_book_confirm:{e}")
    if not booked:
        upsert_lead(psid, confirm_pending="")
        return handoff_to_human(psid, reason="try_book_confirm:empty")
    upsert_lead(psid, confirm_pending="")
    log.info("BOOKED via client Continue/SI psid=%s wo_path=submit-order", psid)
    return _scrub_client_reply(booked, get_lead(psid))


def run_agent(psid: str, text: str, image_urls: list | None = None) -> str:
    upsert_lead(psid)
    lead = get_lead(psid)
    user_text = text or ""
    if image_urls:
        user_text = (user_text + " [el cliente envio foto del cristal]").strip()

    # Ya paso a humano → silencio, excepto si manda datos utiles: el bot los
    # reconoce, se despierta y empuja horarios si ya tiene auto + ZIP.
    if is_bot_muted(lead):
        log.info("bot muted psid=%s stage=%s — skip", psid, lead.get("stage"))
        if user_text:
            append_hist(psid, "user", user_text or "[foto]")
        analysis_muted = analyze_client_message(user_text or text or "", lead)
        if not analysis_muted.get("is_opt_out"):
            lead = _capture_zip(psid, lead, analysis_muted, user_text or text or "")
            lead = _capture_vehicle(psid, lead, analysis_muted, user_text or text or "")
            lead = get_lead(psid) or lead
        if _slots_ready(lead) and not _already_showed_slots(lead) and not lead.get("work_order"):
            upsert_lead(psid, bot_muted="", stage="calificando", slots_error="")
            lead = get_lead(psid) or lead
            try:
                options, slots_err = ensure_slots_for_lead(psid, lead, force=True)
            except Exception:
                log.exception("muted slots")
                options, slots_err = [], "exception"
            if options:
                pitch = _taller_offer_for_message(lead, options)
                reply = _slots_reply_and_handoff(
                    psid,
                    lead,
                    options,
                    mode=(lead.get("service_mode") or "domicilio"),
                    pitch=pitch,
                    user_text=user_text or text or "",
                )
                append_hist(psid, "assistant", reply)
                return reply
            reply = _sin_horarios_reply(psid, lead, slots_err)
            append_hist(psid, "assistant", reply)
            return reply
        if _has_vehicle(lead) and not _lead_zip(lead) and not _already_asked(lead, "zip"):
            upsert_lead(psid, bot_muted="", stage="calificando", slots_error="")
            reply = _ask_zip_now(lead, user_text or text or "")
            append_hist(psid, "assistant", reply)
            return reply
        if wants_price(user_text) and not lead.get("work_order"):
            reply = _scrub_client_reply(_price_only_line(lead, user_text), lead)
            append_hist(psid, "assistant", reply)
            return reply
        return ""

    # limpia basura vieja (auto guardado como "address")
    lead = _clear_garbage_address(psid, lead)
    # completar make desde model si falta (persistir)
    fixed0 = _complete_vehicle_fields(lead)
    if fixed0.get("make") and not (lead.get("make") or "").strip():
        upsert_lead(psid, make=fixed0["make"])
        lead = get_lead(psid)
    if fixed0.get("model") and (lead.get("model") or "") != fixed0.get("model"):
        # normalizar CX-7 etc.
        if re.search(r"(?i)^cx", fixed0.get("model") or ""):
            upsert_lead(psid, model=fixed0["model"], make=fixed0.get("make") or lead.get("make") or "")
            lead = get_lead(psid)

    # ========== CONFIRMAR = Continue en safelite.com/fmg/schedule ==========
    # Solo con SI del cliente se hace submit-order (cita REAL). Nunca antes.
    if (
        (lead.get("confirm_pending") or "") == "1"
        and _ready(lead)
        and not lead.get("work_order")
        and (user_text or "").strip()
    ):
        if _client_confirms_continue(user_text):
            reply = _execute_confirmed_book(psid, lead)
            append_hist(psid, "user", user_text or "[foto]")
            append_hist(psid, "assistant", reply)
            return reply
        if _client_rejects_confirm(user_text) or _wants_reschedule(user_text):
            # liberar slot elegido; pedir otro dia (sin submit)
            upsert_lead(
                psid,
                confirm_pending="",
                slot_id="",
                slot_date="",
                slot_time="",
                stage="calificando",
            )
            reply = "Ok, no lo dejo fijo. ¿Qué otro día o hora te sirve?"
            append_hist(psid, "user", user_text or "[foto]")
            append_hist(psid, "assistant", reply)
            return reply
        # mensaje ambiguo → re-mostrar recap, sin agendar
        reply = _ask_confirm_schedule(psid, lead)
        append_hist(psid, "user", user_text or "[foto]")
        append_hist(psid, "assistant", reply)
        return reply

    # ========== ANALISTA (antes de LLM / geocode / slots) ==========
    analysis = analyze_client_message(user_text or text or "", lead)
    allow = set(analysis.get("allow_fields") or [])
    addr_input_this_turn = False

    # Capturar auto y zip vengan como vengan (auto+zip juntos, "29349.",
    # "del 15", ciudad sin zip). El LLM solo entra si el regex no pudo.
    if not analysis.get("is_opt_out"):
        _msg = user_text or text or ""
        # si el bot habia preguntado "¿Silverado C1500 o C2500?", esto lo resuelve
        if _capture_model_choice(psid, lead, _msg):
            lead = get_lead(psid)
        lead = _capture_zip(psid, lead, analysis, _msg)
        lead = _capture_vehicle(psid, lead, analysis, _msg)

    # Cliente se va / no quiere → 1 frase corta, sin insistir
    # Ya mandamos horarios: cualquier respuesta → pedir tel y apagar.
    if _already_showed_slots(lead) and not lead.get("work_order"):
        try:
            raw = lead.get("slots_json") or ""
            options = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or [])
            if isinstance(options, list) and options:
                result = interpret_slot_request(user_text or "", options, lead=lead)
                if result.get("status") == "matched" and result.get("slot"):
                    _save_slot_choice(psid, result["slot"])
                    lead = get_lead(psid)
        except Exception:
            log.exception("post_horarios_parse")
        phone = _scrub_client_reply(_note_slot_for_human(lead, user_text), lead)
        if wants_price(user_text, analysis):
            reply = f"{_scrub_client_reply(_price_only_line(lead, user_text), lead)}\n{phone}".strip()
        else:
            reply = phone
        append_hist(psid, "user", user_text or "[foto]")
        append_hist(psid, "assistant", reply)
        try:
            _mute_for_human(psid, reason="post_horarios_pide_tel")
        except Exception:
            log.exception("mute post_horarios")
        return reply

    if analysis.get("is_opt_out") and not lead.get("work_order"):
        lang = _client_lang(lead, user_text, psid=psid)
        reply = _t(
            lang,
            "Listo, sin problema. Cuando quieras me escribes.",
            "All good, no problem. Message me whenever you want.",
        )
        upsert_lead(psid, stage="perdido")
        lead = _clear_garbage_address(psid, get_lead(psid))
        append_hist(psid, "user", user_text or "[foto]")
        append_hist(psid, "assistant", reply)
        return reply

    # ========== ROUTER FAQ (antes del LLM) — respuestas super limpias ==========
    # Cristal no-parabrisas, pago, precio, saludo, te aviso, garantia, foto...
    is_price_turn = (
        analysis.get("primary") == "price"
        or analysis.get("is_price")
        or (
            re.search(
                r"(?i)\b(cuanto|cuánto|precio|costo|how much|price|vale|cotiz|reemplaz)\b",
                user_text or "",
            )
            and not analysis.get("is_vehicle")
            and not analysis.get("is_address")
            and not analysis.get("is_schedule")
            and not analysis.get("is_payment")
        )
    )
    is_faq_turn = (
        analysis.get("primary") in ("payment", "price", "later", "greeting")
        or is_price_turn
        or analysis.get("is_greeting")
        or analysis.get("is_payment")
        or analysis.get("is_later")
        or _is_non_windshield_glass(user_text or "")
        or re.search(r"(?i)\b(garant[ií]a|warranty|zelle|cash\s*app|apple\s*pay)\b", user_text or "")
        or bool(_faq_extra_detectar(user_text or ""))
    )
    if not lead.get("work_order") and is_faq_turn:
        if is_price_turn and analysis.get("primary") not in ("payment", "later"):
            analysis = {**analysis, "primary": "price", "is_price": True}
        # saludo solo si no trae auto/zip en el mismo mensaje
        if analysis.get("is_greeting") and not analysis.get("is_vehicle") and not analysis.get("is_address"):
            analysis = {**analysis, "primary": "greeting"}
        human = reply_like_human({**lead, "psid": psid}, user_text or text or "", analysis)
        if human:
            reply = _scrub_client_reply(human, lead)
            append_hist(psid, "user", user_text or "[foto]")
            append_hist(psid, "assistant", reply)
            return reply

    # Cliente aclara que NO dio / no da direccion → no hostigar; sigue la venta
    if analysis.get("is_refuse_address") and not lead.get("work_order"):
        lead = _clear_garbage_address(psid, lead)
        lang = _client_lang(lead, user_text, psid=psid)
        if _has_vehicle(lead):
            pl = price_line(
                lead.get("year"), lead.get("make") or "", lead.get("model") or "",
                lead=lead, psid=psid, lang=lang,
            )
            reply = _t(
                lang,
                f"Ok, sin problema. Queda en {pl}. Cuando quieras lo agendamos.",
                f"Ok, no problem. It's {pl}. We can book whenever you're ready.",
            )
        else:
            reply = _t(
                lang,
                f"Ok, sin problema. El cambio queda en {PRECIO_AUTO} con garantia. Cuando quieras seguimos.",
                f"Ok, no problem. Replacement is {PRECIO_AUTO} with warranty. We can continue whenever.",
            )
        append_hist(psid, "user", user_text or "[foto]")
        append_hist(psid, "assistant", reply)
        return reply

    # ¿Dónde están / ubicación? → domicilio + avanza (no inventar tienda)
    if re.search(
        r"(?i)\b(donde est[aá]n|d[oó]nde est[aá]n|ubicad|localiz|en qu[eé] ciudad est|"
        r"tienen (local|sucursal|tienda)|where (are you|located))\b",
        user_text or "",
    ) and not lead.get("work_order"):
        # no es el unico tema si tambien manda auto/dir; si es chat de ubicacion, corto
        if not analysis.get("is_address") and not (
            analysis.get("is_vehicle") and lead.get("year") and lead.get("make") and lead.get("model")
            and analysis.get("primary") == "vehicle"
        ):
            reply = _scrub_client_reply(_reply_location_short(lead, user_text), lead)
            append_hist(psid, "user", user_text or "[foto]")
            append_hist(psid, "assistant", reply)
            return reply

    # preferencia domicilio / sucursal (solo si el analista lo vio)
    pref = detect_service_pref(text or "")
    if pref and "service_mode" in allow:
        upsert_lead(psid, service_mode=pref)
        lead = get_lead(psid)

    # Extrae SOLO lo que el analista clasifico (gate anti-mezcla)
    if analysis.get("is_vehicle") and analysis.get("vehicle"):
        vf = {}
        for k in ("year", "make", "model"):
            v = (analysis["vehicle"].get(k) or "").strip()
            if v and k in allow:
                vf[k] = v
        if vf:
            # completar make desde modelo (Tacoma→Toyota, etc.)
            merged = _complete_vehicle_fields(lead, vf)
            for k in ("year", "make", "model"):
                if merged.get(k) and not lead.get(k):
                    vf[k] = merged[k]
                elif merged.get(k) and k == "make" and not (lead.get("make") or "").strip():
                    vf[k] = merged[k]
            if not vf.get("make") and merged.get("make"):
                vf["make"] = merged["make"]
            upsert_lead(psid, **{k: v for k, v in vf.items() if v})
            lead = get_lead(psid)
            # segunda pasada por si el lead ya tenia model sin make
            fixed = _complete_vehicle_fields(lead)
            if fixed.get("make") and not (lead.get("make") or "").strip():
                upsert_lead(psid, make=fixed["make"])
                lead = get_lead(psid)
            log.info("vehicle locked from analyzer psid=%s %s", psid, vf)
            # Respuesta inmediata al auto (sin LLM): precio + zip o horarios
            if _has_vehicle(lead) and not lead.get("work_order"):
                if _lead_zip(lead):
                    # dejar que el bloque de slots LIVE conteste con horarios
                    pass
                else:
                    reply = _scrub_client_reply(
                        _reply_vehicle_short(lead, user_text or text or ""), lead
                    )
                    append_hist(psid, "user", user_text or "[foto]")
                    append_hist(psid, "assistant", reply)
                    return reply

    if analysis.get("is_phone") and analysis.get("phone"):
        upsert_lead(psid, phone=analysis["phone"], client_phone=analysis["phone"])
        lead = get_lead(psid)

    # ========== RESPUESTAS CORTAS (sin LLM, sin labia) ==========
    # (precio ya salio arriba en FAQ — no duplicar)

    # Dio el auto (y aun no hay ZIP para horarios) → pedir SOLO zip. Si ya hay zip, sigue a slots.
    if (
        analysis.get("is_vehicle")
        and not analysis.get("is_address")
        and not analysis.get("is_schedule")
        and not lead.get("work_order")
        and lead.get("year")
        and lead.get("make")
        and lead.get("model")
        and not _slots_ready(lead)
    ):
        reply = _reply_vehicle_short(lead, user_text)
        append_hist(psid, "user", user_text or "[foto]")
        append_hist(psid, "assistant", reply)
        return reply

    # Solo saludo → 1 linea (si ya hay auto, no lo pide de nuevo)
    if analysis.get("primary") == "greeting" and not lead.get("work_order"):
        reply = _reply_greeting_short(lead, user_text)
        append_hist(psid, "user", user_text or "[foto]")
        append_hist(psid, "assistant", reply)
        return reply

    if analysis.get("is_address"):
        z = analysis.get("zip") or _extract_zip(text or "")
        if z:
            # siempre guardar zip (incluso si ya habia uno distinto: cliente cambia zona)
            prev_z = _lead_zip(lead)
            if z != prev_z:
                upsert_lead(
                    psid,
                    zip=z,
                    slots_json="",
                    slots_fetched_at="",
                    slot_id="" if z != prev_z else (lead.get("slot_id") or ""),
                    slot_date="" if z != prev_z else (lead.get("slot_date") or ""),
                    slot_time="" if z != prev_z else (lead.get("slot_time") or ""),
                )
            elif not lead.get("zip"):
                upsert_lead(psid, zip=z)
            addr_input_this_turn = True
        if analysis.get("address_raw") and _looks_like_address(analysis["address_raw"]):
            prev = (lead.get("address") or "").strip().lower()
            new_a = analysis["address_raw"].strip()[:160]
            if new_a.lower() != prev:
                upsert_lead(
                    psid,
                    address=new_a,
                    address_verified="",
                    address_formatted="",
                    lat="",
                    lng="",
                    slots_json="",
                    slots_fetched_at="",
                    slot_id="",
                    slot_date="",
                    slot_time="",
                )
                addr_input_this_turn = True
            elif not lead.get("address"):
                upsert_lead(psid, address=new_a)
                addr_input_this_turn = True
        lead = get_lead(psid)
        # FAST PATH: auto + zip (sin calle) → ir YA a horarios LIVE, sin LLM de relleno
        if (
            _slots_ready(lead)
            and not _has_slot(lead)
            and not lead.get("work_order")
            and not (analysis.get("address_raw") and _looks_like_address(analysis.get("address_raw") or ""))
        ):
            try:
                zc = _lead_zip(lead)
                zinfo = safelite_zip(zc) or {}
                if zinfo:
                    upsert_lead(
                        psid,
                        city=lead.get("city") or zinfo.get("city") or "",
                        state=lead.get("state") or zinfo.get("state") or "",
                    )
                    lead = get_lead(psid)
            except Exception:
                log.exception("fast zip city/state")
            try:
                options, slots_err = ensure_slots_for_lead(psid, lead, force=True)
                lead = get_lead(psid)
            except Exception:
                log.exception("fast path slots")
                options, slots_err = [], "exception"
            if not options:
                reply = _sin_horarios_reply(psid, lead, slots_err)
                append_hist(psid, "user", user_text or "[foto]")
                append_hist(psid, "assistant", reply)
                return reply
            pref_now = detect_service_pref(text or "")
            if pref_now == "sucursal":
                upsert_lead(psid, service_mode="sucursal")
                mode = "sucursal"
            else:
                upsert_lead(psid, service_mode=lead.get("service_mode") or "domicilio")
                mode = lead.get("service_mode") or "domicilio"
            pitch = _taller_offer_for_message(lead, options)
            reply = _slots_reply_and_handoff(
                psid, lead, options, mode=mode if mode == "sucursal" else "domicilio",
                pitch=pitch, user_text=text or user_text or "",
            )
            append_hist(psid, "user", user_text or "[foto]")
            append_hist(psid, "assistant", reply)
            return reply
    # si el mensaje es AUTO y no dir → prohibido tocar address este turno
    if analysis.get("is_vehicle") and not analysis.get("is_address"):
        addr_input_this_turn = False

    # already booked — comodidad: recap / cambio / cancel + nueva
    if lead.get("work_order"):
        reply = _scrub_client_reply(handle_post_booking(psid, lead, text or ""))
        append_hist(psid, "user", user_text or "[foto]")
        append_hist(psid, "assistant", reply)
        return reply

    # Quiere dia/horario pero falta ZIP (o auto) → bloquear solo con lo minimo
    wants_when = bool(
        analysis.get("is_schedule")
        or _parse_when_intent(user_text or "").get("has_when")
        or re.search(
            r"(?i)\b(que dia|qu[eé] d[ií]a|horario|disponib|esta semana|ma[nñ]ana|"
            r"hoy|asap|lo mas pronto|cuando pueden|agenda)\b",
            user_text or "",
        )
    )
    if (
        wants_when
        and not _slots_ready(lead)
        and not lead.get("work_order")
        and not analysis.get("is_address")
    ):
        need = "vehicle" if not _has_vehicle(lead) else "zip"
        reply = _scrub_client_reply(_blocker_need(lead, need, text=user_text), lead)
        append_hist(psid, "user", user_text or "[foto]")
        append_hist(psid, "assistant", reply)
        return reply

    # Preferencia taller: "lo llevo/yebo" o OK mañana tras oferta de taller (Angel Oliva)
    pref_now = detect_service_pref(user_text or "")
    if pref_now == "sucursal" or _client_accepts_taller_manana(user_text or "", lead):
        upsert_lead(psid, service_mode="sucursal")
        lead = get_lead(psid)
        pref_now = "sucursal"
    elif pref_now == "domicilio":
        upsert_lead(psid, service_mode="domicilio")
        lead = get_lead(psid)

    # --- Interpretar dia/hora vs disponibilidad LIVE de Safelite ---
    if _slots_ready(lead) and not _has_slot(lead):
        options = []
        mode_now = (lead.get("service_mode") or "domicilio").lower()
        # TALLER: consultar shop-time-slots (no re-spam domicilio)
        if mode_now == "sucursal" or pref_now == "sucursal":
            try:
                shop_opts = safelite_shop_slots(lead, days=14)
            except Exception:
                log.exception("shop slots choice")
                shop_opts = []
            if shop_opts:
                intent = _parse_when_intent(text or "")
                day_shop = [o for o in shop_opts if _slot_matches_day(o, intent)] if intent.get("has_when") else shop_opts
                use = day_shop or shop_opts
                # si pide hora/dia y hay match → anotar
                result = interpret_slot_request(text or "", use, lead=lead)
                if result["status"] == "matched" and result.get("slot"):
                    upsert_lead(psid, service_mode="sucursal")
                    reply = _apply_slot_and_continue(psid, get_lead(psid), result["slot"], user_text)
                    append_hist(psid, "user", user_text or "[foto]")
                    append_hist(psid, "assistant", reply)
                    return reply
                body = _format_availability_live(use, limit_days=2)
                first = _first_slot_date(use)
                first_txt = _fmt_day(first.isoformat()) if first else ""
                if intent.get("relative") == "manana" and day_shop:
                    reply = f"Taller mañana:\n{body}\n¿Qué hora te acomoda? (tú lo llevas)"
                elif first_txt:
                    reply = f"Taller — lo más pronto es el {first_txt}:\n{body}\n¿Qué día/hora te acomoda en taller?"
                else:
                    reply = f"Horarios en taller:\n{body}\n¿Cuál te sirve?"
                reply = _scrub_client_reply(reply, lead)
                append_hist(psid, "user", user_text or "[foto]")
                append_hist(psid, "assistant", reply)
                return reply

        # cache fresca si el cliente esta eligiendo; si no hay, se refresca abajo
        if lead.get("slots_json"):
            try:
                options = json.loads(lead["slots_json"])
            except Exception:
                options = []
        # si el cliente esta pidiendo un dia/hora y no hay cache, ir LIVE ya
        if (not options) and text and _parse_when_intent(text).get("has_when"):
            try:
                options, slots_err = ensure_slots_for_lead(psid, lead, force=True)
                lead = get_lead(psid)
                if not options and slots_err:
                    reply = _sin_horarios_reply(psid, lead, slots_err)
                    append_hist(psid, "user", user_text or "[foto]")
                    append_hist(psid, "assistant", reply)
                    return reply
            except Exception:
                log.exception("live slots on choice")
                reply = _sin_horarios_reply(psid, lead, "exception")
                append_hist(psid, "user", user_text or "[foto]")
                append_hist(psid, "assistant", reply)
                return reply
        if options:
            result = interpret_slot_request(text or "", options, lead=lead)
            if result["status"] == "matched" and result.get("slot"):
                reply = _apply_slot_and_continue(psid, lead, result["slot"], user_text)
                append_hist(psid, "user", user_text or "[foto]")
                append_hist(psid, "assistant", reply)
                return reply
            if result["status"] in ("day_unavailable", "time_unavailable", "need_clarify") and result.get("message"):
                # si ofreció taller en el mensaje, marcar mode
                if re.search(r"(?i)\btaller\b", result.get("message") or ""):
                    # no forzar mute; cliente puede elegir
                    pass
                reply = _scrub_client_reply(result["message"], lead)
                append_hist(psid, "user", user_text or "[foto]")
                append_hist(psid, "assistant", reply)
                return reply
            # "asap" / lo mas pronto → primer hueco REAL del API
            if result["status"] == "no_intent" and re.search(
                r"\b(lo mas pronto|lo antes|primer hueco|el primero|cuando sea)\b",
                _norm_es(text or ""),
            ):
                reply = _apply_slot_and_continue(psid, lead, options[0], user_text)
                append_hist(psid, "user", user_text or "[foto]")
                append_hist(psid, "assistant", reply)
                return reply

    # asegurar psid en lead para bloquear precio
    if psid and not lead.get("psid"):
        lead = {**lead, "psid": psid}
    precio_sug = quote_price(
        lead.get("year"), lead.get("make") or "", lead.get("model") or "", text or "",
        lead=lead, psid=psid,
    )
    mode = lead.get("service_mode") or "domicilio"
    addr_gaps = _address_gaps(lead)
    how_addr = _how_to_ask_address(lead)
    style_hint = _client_style_hint(psid, user_text)
    lang = _client_lang(lead, user_text, psid=psid)
    ctx = {
        "name": lead.get("name"),
        "phone": lead.get("phone"),
        "year": lead.get("year"),
        "make": lead.get("make"),
        "model": lead.get("model"),
        "zip": lead.get("zip"),
        "address": lead.get("address"),
        "city": lead.get("city"),
        "state": lead.get("state"),
        "missing": _missing(lead),
        "address_gaps": addr_gaps,
        "how_to_ask_address": how_addr,
        "address_verified": lead.get("address_verified") or "",
        "address_formatted": lead.get("address_formatted") or "",
        "idioma_cliente": lang,
        "reply_language": "english" if lang == "en" else "spanish",
        "estilo_cliente": style_hint,
        "analisis_mensaje": {
            "primary": analysis.get("primary"),
            "kinds": analysis.get("kinds"),
            "es_auto": analysis.get("is_vehicle"),
            "es_direccion": analysis.get("is_address"),
            "es_precio": analysis.get("is_price"),
            "vehiculo_detectado": analysis.get("vehicle") or {},
            "campos_permitidos": analysis.get("allow_fields") or [],
        },
        "core_ready": _slots_ready(lead),
        "slots_ready": _slots_ready(lead),
        "zip_listo": bool(_lead_zip(lead)),
        "calle_lista": _street_ready(lead),
        "tiene_horario": _has_slot(lead),
        "esperando_horario": _slots_ready(lead) and not _has_slot(lead),
        "service_mode": mode,
        "ya_agendado": bool(lead.get("work_order")),
        "precio_sugerido": precio_sug,
        "precio_texto": price_line(
            lead.get("year"), lead.get("make") or "", lead.get("model") or "", text or "",
            lead=lead, psid=psid, lang=lang,
        ),
        "precios_regla": (
            f"PRECIO con $: usa '{precio_sug}'. Siempre el signo (220$). "
            f"Base {PRECIO_AUTO}$. Precio es secundario: prioriza ZIP y horarios."
        ),
        "ya_pedi_dir": _already_asked(lead, "address"),
        "ya_pedi_zip": _already_asked(lead, "zip") or _already_asked(lead, "address"),
        "ya_pedi_auto": _already_asked(lead, "vehicle"),
        "slot_date": lead.get("slot_date"),
        "slot_time": lead.get("slot_time"),
        "offer_taller": False,
        "regla": (
            f"IDIOMA OBLIGATORIO del reply: {'ENGLISH' if lang == 'en' else 'SPANISH'}. "
            "Natural 1-2 frases. MISION: auto+ZIP → horarios LIVE. NADA MAS. "
            "Si falta zip y hay auto: pide SOLO el codigo postal con ESTA frase: "
            "'Envíeme su código postal para revisar los agentes disponibles en su zona.' "
            "PROHIBIDO resumir el carro ('Ok, Honda Civic 2012') sin pedir el zip. "
            "Si el cliente dice fin de semana / un dia: reconocele eso Y pide el zip. "
            "PROHIBIDO pedir calle, direccion, nombre o telefono. Si hace falta contacto: puedes llamarnos por Facebook. "
            "Si ya_pedi_auto=true y no hay auto: NO repitas 'que carro tienes'. "
            "Si es_auto: no trates el msg como direccion. "
            "olvidalo → ok no problem. HORARIOS solo API. Nunca Safelite/links/menus."
        ),
    }

    messages = [
        {"role": "system", "content": _system_prompt()},
        {
            "role": "system",
            "content": "CONTEXTO LEAD (no lo repitas): " + json.dumps(ctx, ensure_ascii=False),
        },
        {
            "role": "system",
            "content": (
                "ANALISIS DEL ULTIMO MENSAJE (OBLIGATORIO): "
                + json.dumps(
                    {
                        "primary": analysis.get("primary"),
                        "kinds": analysis.get("kinds"),
                        "es_auto": analysis.get("is_vehicle"),
                        "es_direccion": analysis.get("is_address"),
                        "vehiculo": analysis.get("vehicle") or {},
                        "regla": (
                            "Si es_auto y no es_direccion: year/make/model si aplica, "
                            "address/city/state/zip SIEMPRE vacios. No digas que la direccion esta mal."
                        ),
                    },
                    ensure_ascii=False,
                )
            ),
        },
        {
            "role": "system",
            "content": "ESTILO DEL CLIENTE (espeja esto en el reply): " + style_hint,
        },
    ]
    for m in history(psid)[-12:]:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": user_text or "Hola"})

    llm_truncated = False
    try:
        raw, finish = _llm(messages, max_tokens=480)
        if finish == "length":
            log.warning("llm finish_reason=length → reintento con mas tokens")
            raw2, finish2 = _llm(messages, max_tokens=700)
            raw, finish = raw2, finish2
            llm_truncated = finish == "length"
        data = _parse(raw)
        if data.pop("_truncated_json", False):
            llm_truncated = True
        # reply del JSON tambien puede venir a medias aunque finish sea stop
        if _looks_truncated((data.get("reply") or "").strip()):
            llm_truncated = True
    except Exception as e:
        log.exception("llm")
        # sin LLM: regla de negocio (precio / zip / next missing) — no "Un momento"
        fb = _rule_fallback_reply(lead, user_text or text or "", analysis)
        data = {"reply": fb, "stage": "calificando"}
        llm_truncated = False

    fields = {}
    # no pisar year/make si el mensaje es solo sobre dia/hora
    when_only = (
        (analysis.get("is_schedule") or _parse_when_intent(text or "").get("has_when"))
        and _slots_ready(lead)
        and not analysis.get("is_vehicle")
    )
    # gate: solo campos que el ANALISTA permite este turno (+ stage)
    # si el LLM inventa address con mensaje de auto → se tira
    for k in ("year", "make", "model", "zip", "address", "city", "state", "phone", "name", "glass"):
        if when_only and k in ("year", "make", "model"):
            continue
        v = (data.get(k) or "").strip()
        if not v:
            continue
        # GATE DURO por tipo de mensaje
        if analysis.get("is_vehicle") and not analysis.get("is_address") and k in (
            "address", "zip", "city", "state",
        ):
            log.info("BLOCK llm field %s (msg is vehicle) psid=%s val=%r", k, psid, v[:60])
            continue
        if analysis.get("is_address") is False and k == "address":
            # sin señal de dir en el mensaje, no aceptar address del LLM
            if not _looks_like_address(v):
                log.info("BLOCK llm address (no address signal) psid=%s val=%r", psid, v[:60])
                continue
        if k == "phone":
            v = _extract_phone(v) or v
        if k == "zip":
            v = _extract_zip(v) or v
            if analysis.get("is_vehicle") and not analysis.get("is_address"):
                continue
        if k == "address":
            if not _looks_like_address(v) or _looks_like_vehicle_blob(v):
                log.info("llm address basura ignorada psid=%s val=%r", psid, v[:80])
                continue
        if k in ("city", "state") and _looks_like_vehicle_blob(v):
            continue
        # si el analista ya saco year/make/model, no dejar que LLM pise con basura
        if k in ("year", "make", "model") and analysis.get("vehicle", {}).get(k):
            # preferir el del analista
            v = analysis["vehicle"][k]
        fields[k] = v
    if fields.get("phone"):
        fields["client_phone"] = fields["phone"]
    sm = (data.get("service_mode") or "").strip().lower()
    if sm in ("domicilio", "sucursal"):
        fields["service_mode"] = sm
    vs = (data.get("vehicle_size") or "").strip().lower()
    if vs == "grande" or _is_large_vehicle(
        fields.get("make") or lead.get("make") or "",
        fields.get("model") or lead.get("model") or "",
        user_text,
    ):
        if not (fields.get("style") or lead.get("style")):
            fields["style"] = "grande"
    # cliente se va: respetar stage perdido del LLM sin forzar dir
    stage_now = (data.get("stage") or "").strip().lower()
    if stage_now == "perdido" or analysis.get("is_opt_out"):
        fields["stage"] = "perdido"
    elif data.get("stage") and data.get("stage") != "ganado":
        fields["stage"] = data["stage"]
    elif data.get("stage") == "cerrando":
        fields["stage"] = "cerrando"
    if fields:
        # si el LLM trae address/zip/city/state reales → revalidar
        addr_keys = {"address", "zip", "city", "state"}
        if addr_keys & set(fields.keys()) and analysis.get("is_address"):
            fields["address_verified"] = ""
            fields.setdefault("slots_json", "")
            addr_input_this_turn = True
        elif addr_keys & set(fields.keys()) and not analysis.get("is_address"):
            # tirar keys de dir si el mensaje no era dir
            for ak in list(addr_keys):
                fields.pop(ak, None)
            fields.pop("address_verified", None)
        if fields:
            upsert_lead(psid, **fields)
    lead = get_lead(psid)
    lead = _clear_garbage_address(psid, lead)

    # stage perdido del LLM → usar reply corto del LLM, no forzar direccion
    if (lead.get("stage") or "").strip().lower() == "perdido" and not lead.get("work_order"):
        reply = (data.get("reply") or "Listo, sin problema. Cualquier cosa me escribes.").strip()
        reply = _scrub_client_reply(reply)
        # si el LLM se puso pesado pidiendo dir, achicar
        if re.search(r"direcci[oó]n|calle|zip", reply, re.I) and (
            analysis.get("is_opt_out") or _client_wants_out(user_text or "")
        ):
            reply = "Listo, sin problema. Cualquier cosa me escribes."
        append_hist(psid, "user", user_text or "[foto]")
        append_hist(psid, "assistant", reply)
        return reply

    booked_msg = None
    mode = lead.get("service_mode") or "domicilio"

    # 0) Bot NO valida ni pide calle. Solo ZIP → horarios. (Calle = humano a mano.)
    has_auto = bool(lead.get("year") and lead.get("make") and lead.get("model"))
    if _lead_zip(lead) and has_auto and not lead.get("work_order"):
        zc = _lead_zip(lead)
        try:
            if not (lead.get("city") and lead.get("state")):
                zinfo = safelite_zip(zc) or {}
                if zinfo:
                    upsert_lead(
                        psid,
                        zip=zc,
                        city=lead.get("city") or zinfo.get("city") or "",
                        state=lead.get("state") or zinfo.get("state") or "",
                    )
                    lead = get_lead(psid)
        except Exception:
            log.exception("zip city/state fill")
    # si el cliente manda basura como "address", limpiar sin hostigar
    raw_addr = (lead.get("address") or "").strip()
    if raw_addr and not _looks_like_address(raw_addr):
        lead = _clear_garbage_address(psid, lead)

    # 1) Auto + ZIP → horarios LIVE SOLO cuando toca (no spamear en cada FAQ).
    #    Aprendizaje Ramon Sanchez: precio/pago/"te aviso" NO re-envian la lista.
    if _slots_ready(lead) and not _has_slot(lead) and not lead.get("work_order"):
        already = _already_showed_slots(lead)
        just_got_zip = bool(
            analysis.get("is_address")
            and _lead_zip(lead)
            and not already
        )
        asks_again = bool(
            re.search(
                r"(?i)\b(horario|horarios|disponib|que dias|qu[eé] d[ií]as|"
                r"otra vez los dias|mandame los|env[ií]ame horarios)\b",
                user_text or "",
            )
        )
        wants_slots_list = (
            just_got_zip
            or (not already)
            or bool(analysis.get("is_schedule"))
            or asks_again
        )
        # FAQ / later / payment / pure price confirm → NO lista
        if analysis.get("primary") in ("payment", "later", "price", "opt_out", "refuse_address"):
            if already and not analysis.get("is_schedule"):
                wants_slots_list = False

        if not wants_slots_list and already:
            # ya vio horarios: respuesta corta humana, sin re-listar
            human = reply_like_human({**lead, "psid": psid}, user_text or text or "", analysis)
            if human:
                reply = _scrub_client_reply(human, lead)
            else:
                lang = _client_lang(lead, user_text, psid=psid)
                reply = _t(
                    lang,
                    "¿Qué día te acomoda de los que te pasé?",
                    "Which of the days I sent works for you?",
                )
            append_hist(psid, "user", user_text or "[foto]")
            append_hist(psid, "assistant", reply)
            return reply

        if wants_slots_list:
            slots_err = None
            try:
                options, slots_err = ensure_slots_for_lead(psid, lead, force=True)
                lead = get_lead(psid)
            except Exception:
                log.exception("ensure_slots LIVE")
                options, slots_err = [], "exception"

            if not options:
                reply = _sin_horarios_reply(psid, lead, slots_err)
                append_hist(psid, "user", user_text or "[foto]")
                append_hist(psid, "assistant", reply)
                return reply

            # re-interpretar por si el mensaje de este turno ya trae dia/hora + datos
            if options and text:
                result = interpret_slot_request(text, options, lead=lead)
                if result["status"] == "matched" and result.get("slot"):
                    reply = _apply_slot_and_continue(psid, lead, result["slot"], user_text)
                    append_hist(psid, "user", user_text or "[foto]")
                    append_hist(psid, "assistant", reply)
                    return reply
                if result["status"] in ("day_unavailable", "time_unavailable", "need_clarify") and result.get("message"):
                    reply = _scrub_client_reply(result["message"], lead)
                    append_hist(psid, "user", user_text or "[foto]")
                    append_hist(psid, "assistant", reply)
                    return reply

            pref_now = detect_service_pref(text or "")
            if pref_now == "sucursal":
                upsert_lead(psid, service_mode="sucursal")
                mode = "sucursal"
            elif pref_now == "domicilio" or (lead.get("service_mode") or "") != "sucursal":
                if pref_now == "domicilio" or not lead.get("service_mode"):
                    upsert_lead(psid, service_mode="domicilio")
                mode = lead.get("service_mode") or "domicilio"
                if pref_now == "domicilio":
                    mode = "domicilio"
                    upsert_lead(psid, service_mode="domicilio")
            pitch = _taller_offer_for_message(lead, options)
            if pitch.get("offer_taller"):
                log.info(
                    "offer_taller psid=%s mobile=%s shop=%s days_earlier=%s mode=%s",
                    psid, pitch.get("mobile_first"), pitch.get("shop_first"),
                    pitch.get("taller_days_earlier"), mode,
                )
            reply = _slots_reply_and_handoff(
                psid, lead, options,
                mode=mode if mode == "sucursal" else "domicilio",
                pitch=pitch, user_text=text or user_text or "",
            )
            append_hist(psid, "user", user_text or "[foto]")
            append_hist(psid, "assistant", reply)
            return reply

    # 2) Ya eligio horario → bot no pide nada mas (cierre manual del humano)
    if _has_slot(lead) and not lead.get("work_order"):
        reply = _scrub_client_reply(_note_slot_for_human(lead, user_text), lead)
        append_hist(psid, "user", user_text or "[foto]")
        append_hist(psid, "assistant", reply)
        try:
            _mute_for_human(psid, reason="slot_ya_elegido_manual")
        except Exception:
            pass
        return reply

    # 3) Bot NO hace submit automatico (_ready siempre False). Cierre manual.
    booked_msg = None
    if False and _ready(lead) and not lead.get("work_order"):
        if (lead.get("confirm_pending") or "") == "1" and _client_confirms_continue(user_text):
            reply = _execute_confirmed_book(psid, lead)
            append_hist(psid, "user", user_text or "[foto]")
            append_hist(psid, "assistant", reply)
            return reply
        if (lead.get("confirm_pending") or "") != "1":
            reply = _scrub_client_reply(_ask_confirm_schedule(psid, lead), lead)
            append_hist(psid, "user", user_text or "[foto]")
            append_hist(psid, "assistant", reply)
            return reply
        # confirm_pending=1 pero mensaje no es SI claro → re-mostrar
        reply = _scrub_client_reply(_ask_confirm_schedule(psid, lead), lead)
        append_hist(psid, "user", user_text or "[foto]")
        append_hist(psid, "assistant", reply)
        return reply

    reply = (data.get("reply") or "¿En qué te ayudo?").strip()

    # si el LLM dio vueltas con precio sin numero → fuerza precio directo
    if analysis.get("is_price") or re.search(
        r"(?i)\b(cuanto|cuánto|precio|how much|costo)\b", user_text or ""
    ):
        if not re.search(r"\b(2\d{2}|1\d{2})\b", reply or "") or re.search(
            r"(?i)(qu[eé]\s+carro|a[nñ]o,?\s*marca).{0,40}(precio|cuanto|costo)",
            reply or "",
        ):
            # precio primero, no "dime el carro" sin precio
            reply = _reply_price_short(lead, user_text)

    # Si el LLM pidio auto/dir ya sabidos → limpia. Si quedo vacio, next missing.
    reply = _ensure_complete_reply(reply, lead, truncated_flag=llm_truncated)
    reply = _strip_reask_known(reply, lead)
    if not (reply or "").strip():
        reply = _next_missing_ask(lead)
    # defensa final: con auto ya sabido, NUNCA "qué carro tienes"
    if _has_vehicle(lead) and re.search(
        r"(?i)(qu[eé]\s+carro|a[nñ]o,?\s*marca\s*y\s*modelo)", reply or ""
    ):
        reply = _next_missing_ask(lead)
    reply = _trim_habladero(reply)
    append_hist(psid, "user", user_text or "[foto]")
    append_hist(psid, "assistant", reply)
    return reply


# ---------------------------------------------------------------------------
# FastAPI: dashboard + webhook
# ---------------------------------------------------------------------------
@app.on_event("startup")
def startup():
    init_db()
    # defaults de settings si no existen
    if not get_setting("negocio"):
        set_setting("negocio", NEGOCIO)
    if not get_setting("precio"):
        set_setting("precio", str(PRECIO))
    if not CITAS.exists():
        note("CITAS — generadas por el agente")
        note("Cada bloque es una reserva real.")
        note("")
    log.info("Agente listo | dashboard -> /dashboard | citas -> %s", CITAS)


@app.get("/")
def root():
    return {
        "ok": True,
        "service": "agente-ventas",
        "mode": "api",
        "endpoints": {
            "health": "/",
            "webhook": "/webhook",
            "login": "POST /api/dashboard/login",
            "me": "/api/dashboard/me",
            "leads": "/api/dashboard/leads",
            "lead": "/api/dashboard/leads/{psid}",
            "insights": "/api/dashboard/insights",
            "inbox_live": "/api/dashboard/fb/live",
            "inbox_sync": "POST /api/dashboard/fb/sync",
            "thread": "/api/dashboard/fb/thread/{psid}",
            "agenda": "/api/dashboard/agenda",
            "pool": "/api/dashboard/pool",
            "settings": "/api/dashboard/settings",
            "privacy": "/privacy",
            "terms": "/terms",
            "data_deletion": "/data-deletion",
        },
    }


@app.get("/dashboard")
def dashboard_page():
    return root()


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page():
    """URL publica requerida por Meta para poner la app en Live."""
    negocio = get_setting("negocio", NEGOCIO)
    return HTMLResponse(
        f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<title>Politica de Privacidad — {negocio}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 16px;line-height:1.5;color:#111}}
h1{{font-size:1.5rem}} h2{{font-size:1.1rem;margin-top:1.6rem}}</style></head><body>
<h1>Politica de Privacidad</h1>
<p><strong>{negocio}</strong> opera un asistente de mensajeria en Facebook Messenger
para coordinar servicios de reparacion/instalacion de cristales de vehiculos.</p>
<h2>Datos que recopilamos</h2>
<ul>
<li>Nombre y mensajes que nos envias por Messenger</li>
<li>Telefono y direccion, solo si los compartis para coordinar el servicio</li>
<li>Datos del vehiculo (ano, marca, modelo) y preferencias de cita</li>
</ul>
<h2>Para que los usamos</h2>
<ul>
<li>Responder tus consultas y coordinar citas de servicio</li>
<li>Contactarte sobre tu solicitud (telefono/Messenger)</li>
<li>Mejorar la atencion al cliente</li>
</ul>
<h2>Con quien compartimos datos</h2>
<p>Solo con proveedores necesarios para prestar el servicio (por ejemplo, agenda/tecnicos).
No vendemos tu informacion.</p>
<h2>Retencion</h2>
<p>Conservamos los datos el tiempo necesario para el servicio y obligaciones legales.</p>
<h2>Tus derechos</h2>
<p>Podes pedir acceso, correccion o eliminacion de tus datos escribiendonos por Messenger
o al contacto de la Pagina.</p>
<h2>Contacto</h2>
<p>Pagina de Facebook: Parabrisas solution / {negocio}.</p>
<p>Ultima actualizacion: 2026-07-24</p>
</body></html>"""
    )


@app.get("/terms", response_class=HTMLResponse)
def terms_page():
    negocio = get_setting("negocio", NEGOCIO)
    return HTMLResponse(
        f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<title>Terminos de Servicio — {negocio}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 16px;line-height:1.5;color:#111}}
h1{{font-size:1.5rem}}</style></head><body>
<h1>Terminos de Servicio</h1>
<p>El asistente de Messenger de <strong>{negocio}</strong> brinda informacion y ayuda
a coordinar servicios de cristales/parabrisas. Las citas y precios definitivos
quedan sujetos a confirmacion del negocio.</p>
<p>Al escribirnos aceptas que usemos tus mensajes y datos de contacto para
atender tu solicitud. No uses el servicio para contenido ilegal o spam.</p>
<p>Ultima actualizacion: 2026-07-24</p>
</body></html>"""
    )


@app.post("/data-deletion")
async def data_deletion(request: Request):
    """
    Callback de eliminacion de datos exigido por Meta (Platform Terms).
    Configurar en App Dashboard → Settings → Basic → Data Deletion Request URL:
      https://TU-DOMINIO/data-deletion
    """
    signed = ""
    user_id = None
    raw = await request.body()
    ctype = (request.headers.get("content-type") or "").lower()
    try:
        if "application/json" in ctype:
            body = json.loads(raw.decode() or "{}")
            user_id = str(body.get("user_id") or body.get("psid") or "")
            signed = str(body.get("signed_request") or "")
        else:
            # application/x-www-form-urlencoded (Meta)
            from urllib.parse import parse_qs
            qs = parse_qs(raw.decode() or "")
            signed = (qs.get("signed_request") or [""])[0]
            user_id = (qs.get("user_id") or [""])[0] or None
    except Exception:
        pass

    secret = _app_secret()
    if signed and secret:
        data = parse_signed_request(str(signed), secret)
        if data:
            user_id = str(data.get("user_id") or data.get("user") or user_id or "")

    code = f"del_{int(datetime.utcnow().timestamp())}_{re.sub(r'[^0-9a-zA-Z]', '', user_id or 'anon')[:16]}"
    deleted = False
    if user_id:
        with db() as c:
            cur = c.execute("DELETE FROM leads WHERE psid=?", (user_id,))
            deleted = cur.rowcount > 0
        note(f"[{datetime.utcnow().isoformat()}] DATA_DELETION user_id={user_id} deleted={deleted} code={code}")
    else:
        note(f"[{datetime.utcnow().isoformat()}] DATA_DELETION sin user_id code={code}")

    base = str(request.base_url).rstrip("/")
    status_url = f"{base}/data-deletion/status?code={code}"
    return JSONResponse({"url": status_url, "confirmation_code": code})


@app.get("/data-deletion/status")
def data_deletion_status(code: str = ""):
    return HTMLResponse(
        f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<title>Eliminacion de datos</title></head><body style="font-family:system-ui;max-width:640px;margin:40px auto;padding:0 16px">
<h1>Solicitud de eliminacion recibida</h1>
<p>Codigo de confirmacion: <strong>{code or 'N/A'}</strong></p>
<p>Tus datos asociados al asistente de mensajeria fueron procesados para eliminacion
conforme a nuestra politica de privacidad (hasta 30 dias).</p>
</body></html>"""
    )


@app.post("/api/dashboard/login")
async def dash_login(request: Request):
    import secrets

    body = await request.json()
    user = str(body.get("user") or body.get("username") or "").strip()
    password = str(body.get("password") or "")
    role = None
    if user and DASH_PASS and _cred_ok(user, DASH_USER) and _cred_ok(password, DASH_PASS):
        role = "admin"
    elif user and STAFF_PASS and _cred_ok(user, STAFF_USER) and _cred_ok(password, STAFF_PASS):
        role = "staff"
    elif (not user) and DASH_PASS and _cred_ok(password, DASH_PASS):
        user = DASH_USER
        role = "admin"
    if not role:
        raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")
    token = secrets.token_urlsafe(24)
    _dash_sessions[token] = {"user": user, "role": role}
    return {"token": token, "user": user, "role": role}


@app.get("/api/dashboard/me")
def dash_me(x_dashboard_token: str | None = Header(None)):
    sess = _require_dash(x_dashboard_token)
    return {"user": sess.get("user"), "role": sess.get("role")}


@app.get("/api/dashboard/leads")
def dash_leads(x_dashboard_token: str | None = Header(None)):
    sess = _require_dash(x_dashboard_token)
    leads = list_leads()
    # no mandar history gigante en listado
    slim = []
    for l in leads:
        item = {k: l.get(k) for k in l if k != "history"}
        if sess.get("role") == "staff":
            item = _staff_lead_public(item)
        slim.append(item)
    total = len(leads)
    booked = sum(1 for l in leads if l.get("work_order"))
    ganado = sum(1 for l in leads if l.get("stage") == "ganado")
    perdido = sum(1 for l in leads if l.get("stage") == "perdido")
    finalizado = sum(
        1
        for l in leads
        if (l.get("stage") or "").lower() in ("finalizado", "completado", "done", "finished")
        or l.get("finished_at")
    )
    pipeline = sum(
        1
        for l in leads
        if (l.get("stage") or "nuevo")
        not in ("ganado", "perdido", "finalizado", "completado", "agente")
    )
    conversion = round((booked / total) * 100) if total else 0
    out = {
        "leads": slim,
        "stats": {
            "total": total,
            "booked": booked,
            "ganado": ganado,
            "perdido": perdido,
            "finalizado": finalizado,
            "pipeline": pipeline,
            "conversion": conversion,
        },
    }
    if sess.get("role") == "admin":
        agenda = build_agenda()
        out["agenda"] = agenda
        out["stats"].update({
            "citas_hoy": agenda["counts"]["hoy"],
            "citas_manana": agenda["counts"]["manana"],
            "citas_pasado": agenda["counts"]["pasado_manana"],
            "finalizadas_hoy": agenda["counts"]["finalizadas_hoy"],
            "atrasadas": agenda["counts"]["atrasadas"],
        })
    return out


@app.get("/api/dashboard/leads/{psid}")
def dash_lead_detail(psid: str, x_dashboard_token: str | None = Header(None)):
    sess = _require_dash(x_dashboard_token)
    lead = get_lead(psid)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead no encontrado")
    lead["history"] = json.loads(lead.get("history") or "[]")
    if sess.get("role") == "staff":
        return _staff_lead_public(lead)
    return lead


@app.patch("/api/dashboard/leads/{psid}")
async def dash_lead_update(psid: str, request: Request, x_dashboard_token: str | None = Header(None)):
    _require_dash(x_dashboard_token, admin=True)
    if not get_lead(psid):
        raise HTTPException(status_code=404, detail="Lead no encontrado")
    body = await request.json()
    allowed = {
        "name", "phone", "year", "make", "model", "style", "glass", "zip",
        "address", "city", "state", "slot_date", "slot_time", "work_order",
        "portal", "stage", "admin_note", "bot_muted", "client_phone",
    }
    fields = {k: body[k] for k in allowed if k in body}
    # permitir borrar campos con string vacio
    if fields:
        now = datetime.utcnow().isoformat()
        cols = []
        vals = []
        for k, v in fields.items():
            cols.append(f"{k}=?")
            vals.append(v if v is not None else "")
        cols.append("updated_at=?")
        vals.append(now)
        vals.append(psid)
        with db() as c:
            c.execute(f"UPDATE leads SET {', '.join(cols)} WHERE psid=?", vals)
        # Si el admin marca la cita como finalizada/completada → libera nm/email del pool
        stage = str(fields.get("stage") or "").lower()
        if stage in ("finalizado", "completado", "done", "finished"):
            lead = get_lead(psid)
            free_pool_for_lead(psid, lead.get("work_order") if lead else None)
            now_f = datetime.now().isoformat(timespec="seconds")
            upsert_lead(psid, finished_at=now_f, pool_phone="", pool_email="")
            lead2 = get_lead(psid)
            if lead2:
                note_finalizada(lead2, reason=f"stage={stage}")
    return get_lead(psid)


@app.post("/api/dashboard/leads/{psid}/finalize")
async def dash_lead_finalize(psid: str, x_dashboard_token: str | None = Header(None)):
    """Marca la cita como finalizada, la mueve al apartado de finalizadas y libera pool."""
    _require_dash(x_dashboard_token, admin=True)
    lead = get_lead(psid)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead no encontrado")
    free_pool_for_lead(psid, lead.get("work_order"))
    now_f = datetime.now().isoformat(timespec="seconds")
    upsert_lead(
        psid,
        stage="finalizado",
        finished_at=now_f,
        pool_phone="",
        pool_email="",
    )
    lead2 = get_lead(psid)
    note_finalizada(lead2 or lead, reason="boton Finalizar")
    return {
        "ok": True,
        "lead": lead2,
        "pool": pool_status(),
        "agenda": build_agenda(),
    }


@app.get("/api/dashboard/agenda")
def dash_agenda(x_dashboard_token: str | None = Header(None)):
    """Tablero de citas: hoy / manana / semana / finalizadas — super visible."""
    _require_dash(x_dashboard_token, admin=True)
    return build_agenda()


@app.get("/api/dashboard/pool")
def dash_pool(x_dashboard_token: str | None = Header(None)):
    """Estado del pool nm.txt / email.txt (cuales free / in_use)."""
    _require_dash(x_dashboard_token, admin=True)
    return pool_status()


@app.post("/api/dashboard/leads/{psid}/message")
async def dash_lead_message(psid: str, request: Request, x_dashboard_token: str | None = Header(None)):
    _require_dash(x_dashboard_token)
    if not get_lead(psid):
        raise HTTPException(status_code=404, detail="Lead no encontrado")
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Mensaje vacio")
    from app.flow import force_send_slots, is_force_slots_command

    if is_force_slots_command(text):
        reply = force_send_slots(psid, command_text=text)
        if not reply:
            raise HTTPException(status_code=400, detail="Falta auto o zip para !hora")
        ok = send_msg(psid, reply)
        if not ok:
            raise HTTPException(status_code=502, detail="No se pudo enviar por Messenger")
        return {"ok": True, "command": "!hora"}
    ok = send_msg(psid, text)
    if not ok:
        raise HTTPException(status_code=502, detail="No se pudo enviar por Messenger")
    append_hist(psid, "assistant", text)
    # Tú escribiste desde el panel: el bot no vuelve a hablar en este chat.
    _mute_for_human(psid, reason="humano_dashboard")
    return {"ok": True}


@app.get("/api/dashboard/settings")
def dash_settings_get(x_dashboard_token: str | None = Header(None)):
    sess = _require_dash(x_dashboard_token)
    ms = messenger_status()
    out = {
        "negocio": get_setting("negocio", NEGOCIO),
        "messenger_ok": ms.get("ok"),
        "pagina": ms.get("pagina") if sess.get("role") == "admin" else None,
        "role": sess.get("role"),
        "user": sess.get("user"),
    }
    if sess.get("role") == "admin":
        out.update({
            "precio": get_setting("precio", str(PRECIO)),
            "nota": get_setting("nota", ""),
            "plantilla": get_setting("plantilla", ""),
        })
    return out


@app.post("/api/dashboard/settings")
async def dash_settings_set(request: Request, x_dashboard_token: str | None = Header(None)):
    _require_dash(x_dashboard_token, admin=True)
    body = await request.json()
    for k in ("negocio", "precio", "nota", "plantilla"):
        if k in body:
            set_setting(k, str(body.get(k) or ""))
    return {"ok": True}


@app.get("/api/dashboard/citas")
def dash_citas(x_dashboard_token: str | None = Header(None)):
    _require_dash(x_dashboard_token, admin=True)
    activas = CITAS.read_text(encoding="utf-8") if CITAS.exists() else ""
    finalizadas = CITAS_DONE.read_text(encoding="utf-8") if CITAS_DONE.exists() else ""
    return {
        "text": activas,
        "finalizadas_text": finalizadas,
        "agenda": build_agenda(),
    }


@app.get("/webhook")
def verify(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    if hub_mode == "subscribe" and hub_verify_token == os.getenv("VERIFY_TOKEN"):
        return Response(content=hub_challenge, media_type="text/plain")
    return Response(status_code=403)


def _handle(psid: str, text: str, images: list):
    try:
        # Si ya esta en mano de un humano, no escribas ni marques typing
        lead0 = get_lead(psid)
        if is_bot_muted(lead0):
            log.info("IN muted %s | %s", psid, (text or "")[:80])
            if text:
                append_hist(psid, "user", text)
            return
        typing(psid, True)
        if not get_lead(psid).get("name"):
            n = user_name(psid)
            if n:
                upsert_lead(psid, name=n)
        log.info("IN  %s | %s", psid, text or f"[{len(images)} img]")
        reply = run_agent(psid, text, images)
        typing(psid, False)
        if reply and str(reply).strip():
            send_msg(psid, reply)
            log.info("OUT %s | %s", psid, reply)
        else:
            log.info("OUT %s | (silencio / muted)", psid)
    except Exception:
        typing(psid, False)
        log.exception("handle")
        # error interno → handoff, no spamear "escribime de nuevo"
        try:
            if not is_bot_muted(get_lead(psid)):
                msg = handoff_to_human(psid, reason="handle_exception")
                send_msg(psid, msg)
        except Exception:
            pass


async def _flush(psid: str, seq: int):
    await asyncio.sleep(DEBOUNCE)
    with _lock:
        if _seq[psid] != seq:
            return
        bucket = _pending.pop(psid, None)
    if not bucket:
        return
    texts = [t for t in bucket.get("texts", []) if t]
    images = bucket.get("images", [])
    combined = " | ".join(texts) if len(texts) > 1 else (texts[0] if texts else "")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_executor, _handle, psid, combined, images)


@app.post("/webhook")
async def incoming(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    secret = os.getenv("APP_SECRET")
    if secret:
        sig = request.headers.get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            log.warning("webhook firma invalida")
            return Response(status_code=403)

    try:
        data = json.loads(body.decode() or "{}")
    except Exception:
        log.warning("webhook body no JSON")
        return {"status": "ok"}

    # Log liviano de lo que manda Meta (para diagnosticar)
    n_msg = 0
    for entry in data.get("entry", []):
        # Messenger
        for event in entry.get("messaging", []) or []:
            n_msg += 1
            sender = (event.get("sender") or {}).get("id")
            # a veces el "sender" es la pagina (echo); el user esta en recipient
            page_id = os.getenv("PAGE_ID", "")
            message = event.get("message") or {}
            if message.get("is_echo"):
                log.info("webhook echo ignored from=%s", sender)
                continue
            if event.get("delivery") or event.get("read") or event.get("reaction"):
                log.info("webhook event=%s", list(event.keys()))
                continue
            if not message and not event.get("postback"):
                log.info("webhook other keys=%s", list(event.keys()))
                continue

            # texto / postback / imagen
            text = message.get("text")
            if event.get("postback"):
                text = (event["postback"].get("title") or event["postback"].get("payload") or text)
            images = [
                a["payload"]["url"]
                for a in message.get("attachments", [])
                if a.get("type") == "image" and a.get("payload", {}).get("url")
            ]
            if not text and not images:
                log.info("webhook empty message keys=%s", list(message.keys()))
                continue

            # PSID del cliente: si el sender es la pagina, no es un mensaje de usuario
            psid = sender
            if str(psid) == str(page_id):
                log.info("webhook sender=page ignored")
                continue

            log.info("webhook USER psid=%s text=%s imgs=%s", psid, (text or "")[:80], len(images))
            with _lock:
                b = _pending.get(psid) or {"texts": [], "images": []}
                if text:
                    b["texts"].append(text)
                b["images"].extend(images)
                _pending[psid] = b
                _seq[psid] += 1
                seq = _seq[psid]
            background_tasks.add_task(_flush, psid, seq)

        # a veces Meta manda standby / changes — solo log
        if entry.get("standby"):
            log.info("webhook standby n=%s", len(entry.get("standby") or []))

    if n_msg == 0:
        log.info("webhook sin messaging: keys=%s", list(data.keys()))
    return {"status": "ok"}


# CLI local test: python bot.py chat
if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "chat":
        init_db()
        psid = "local_test"
        print("Chat local (salir = exit). Citas se guardan en citas.txt\n")
        while True:
            msg = input("Cliente: ").strip()
            if msg.lower() in ("exit", "salir", "quit"):
                break
            print("Bot:", run_agent(psid, msg), "\n")
    else:
        import uvicorn

        uvicorn.run("bot:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
