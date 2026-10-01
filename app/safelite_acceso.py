"""
Acceso Safelite por email o teléfono.

El correo de Safelite manda click.t.safelite.com/?qs=...
Eso redirige a myaccount.safelite.com/External/AccountLogin.aspx?payload=...
Ese payload ES el acceso (mismo botón del email).

Esta API no inventa tokens de Safelite. Junta:
  1. Portal (woid + token) de las citas que YA agendamos
  2. Links de correo que pegaste o resolviste
  3. leads.db + citas.txt

GET  /api/safelite/acceso?email=  |  ?phone=  |  ?q=
POST /api/safelite/acceso   {email, phone, url}
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from . import legacy

CLICK_HOST = "click.t.safelite.com"
LOGIN_PATH = "/External/AccountLogin.aspx"
PORTAL_HOST = "myaccount.safelite.com"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def norm_email(v: str | None) -> str:
    return (v or "").strip().lower()


def norm_phone(v: str | None) -> str:
    d = re.sub(r"\D", "", v or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d


def looks_like_email(v: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", (v or "").strip()))


def looks_like_url(v: str) -> bool:
    s = (v or "").strip().lower()
    return s.startswith("http://") or s.startswith("https://")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(legacy.DB)
    c.row_factory = sqlite3.Row
    return c


def init_acceso_table() -> None:
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS safelite_accesos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                name TEXT DEFAULT '',
                woid TEXT DEFAULT '',
                work_order TEXT DEFAULT '',
                portal TEXT DEFAULT '',
                login_url TEXT DEFAULT '',
                click_url TEXT DEFAULT '',
                source TEXT DEFAULT '',
                updated_at TEXT
            )"""
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_acceso_email ON safelite_accesos(email)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_acceso_phone ON safelite_accesos(phone)"
        )
        c.commit()


def unwrap_click(url: str, timeout: float = 18) -> dict[str, str]:
    """Sigue el click.t.safelite.com y saca el login con payload + woid si aparece."""
    raw = (url or "").strip()
    out = {"click_url": "", "login_url": "", "portal": "", "woid": "", "payload": ""}
    if not raw:
        return out
    if CLICK_HOST in raw:
        out["click_url"] = raw
    if LOGIN_PATH in raw and "payload=" in raw:
        out["login_url"] = raw.split("#")[0]
        out["payload"] = _payload_of(raw)
    if "woid=" in raw and "token=" in raw:
        out["portal"] = raw.split("#")[0]
        out["woid"] = _woid_of(raw)
        return out

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False, headers=headers) as client:
            seen: list[str] = []
            cur = raw
            for _ in range(8):
                if cur in seen:
                    break
                seen.append(cur)
                _harvest(cur, out)
                r = client.get(cur)
                loc = r.headers.get("location") or ""
                if loc:
                    nxt = str(httpx.URL(cur).join(loc))
                    _harvest(nxt, out)
                    cur = nxt
                    if r.status_code in (301, 302, 303, 307, 308):
                        continue
                break
    except Exception:
        pass
    return out


def _payload_of(url: str) -> str:
    q = parse_qs(urlparse(url).query)
    vals = q.get("payload") or []
    return unquote(vals[0]) if vals else ""


def _woid_of(url: str) -> str:
    q = parse_qs(urlparse(url).query)
    vals = q.get("woid") or q.get("workOrderId") or []
    if vals:
        return vals[0]
    m = re.search(r"(?:woid|workOrderId)=(\d+)", url, re.I)
    return m.group(1) if m else ""


def _harvest(url: str, out: dict[str, str]) -> None:
    if not url:
        return
    if CLICK_HOST in url and not out["click_url"]:
        out["click_url"] = url
    if LOGIN_PATH in url and "payload=" in url:
        out["login_url"] = url.split("#")[0]
        out["payload"] = _payload_of(url)
    w = _woid_of(url)
    if w:
        out["woid"] = w
    if "token=" in url and "woid=" in url:
        out["portal"] = url.split("#")[0]


def remember(
    *,
    email: str = "",
    phone: str = "",
    name: str = "",
    woid: str = "",
    work_order: str = "",
    portal: str = "",
    login_url: str = "",
    click_url: str = "",
    source: str = "",
) -> dict[str, Any]:
    init_acceso_table()
    email = norm_email(email)
    phone = norm_phone(phone)
    if click_url and not login_url:
        got = unwrap_click(click_url)
        login_url = login_url or got.get("login_url") or ""
        portal = portal or got.get("portal") or ""
        woid = woid or got.get("woid") or ""
        click_url = got.get("click_url") or click_url
    if login_url and not click_url and CLICK_HOST in login_url:
        click_url = login_url
        got = unwrap_click(login_url)
        login_url = got.get("login_url") or login_url
        woid = woid or got.get("woid") or ""
        portal = portal or got.get("portal") or ""

    if not (email or phone or portal or login_url or click_url):
        return {"ok": False, "error": "Nada que guardar"}

    with _conn() as c:
        row = None
        if email:
            row = c.execute(
                "SELECT * FROM safelite_accesos WHERE email=? ORDER BY id DESC LIMIT 1",
                (email,),
            ).fetchone()
        if row is None and phone:
            row = c.execute(
                "SELECT * FROM safelite_accesos WHERE phone=? ORDER BY id DESC LIMIT 1",
                (phone,),
            ).fetchone()
        fields = {
            "email": email,
            "phone": phone,
            "name": name,
            "woid": woid,
            "work_order": work_order,
            "portal": portal,
            "login_url": login_url,
            "click_url": click_url,
            "source": source,
            "updated_at": _now(),
        }
        if row:
            merged = dict(row)
            for k, v in fields.items():
                if v:
                    merged[k] = v
            c.execute(
                """UPDATE safelite_accesos SET
                    email=?, phone=?, name=?, woid=?, work_order=?,
                    portal=?, login_url=?, click_url=?, source=?, updated_at=?
                   WHERE id=?""",
                (
                    merged.get("email") or "",
                    merged.get("phone") or "",
                    merged.get("name") or "",
                    merged.get("woid") or "",
                    merged.get("work_order") or "",
                    merged.get("portal") or "",
                    merged.get("login_url") or "",
                    merged.get("click_url") or "",
                    merged.get("source") or source,
                    _now(),
                    row["id"],
                ),
            )
        else:
            c.execute(
                """INSERT INTO safelite_accesos
                   (email, phone, name, woid, work_order, portal, login_url, click_url, source, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    email, phone, name, woid, work_order,
                    portal, login_url, click_url, source, _now(),
                ),
            )
        c.commit()
    return {"ok": True}


def _row_to_item(r: sqlite3.Row | dict) -> dict[str, Any]:
    d = dict(r)
    login = d.get("login_url") or ""
    portal = d.get("portal") or ""
    click = d.get("click_url") or ""
    acceso = login or portal or click
    return {
        "email": d.get("email") or "",
        "phone": d.get("phone") or "",
        "name": d.get("name") or "",
        "woid": d.get("woid") or "",
        "work_order": d.get("work_order") or "",
        "acceso": acceso,
        "login_url": login,
        "portal": portal,
        "click_url": click,
        "source": d.get("source") or "",
        "updated_at": d.get("updated_at") or "",
    }


def lookup(email: str = "", phone: str = "", q: str = "") -> dict[str, Any]:
    init_acceso_table()
    reindex_quiet()
    raw = (q or "").strip()
    email = norm_email(email)
    phone = norm_phone(phone)

    if raw and looks_like_url(raw):
        got = unwrap_click(raw)
        acceso = got.get("login_url") or got.get("portal") or raw
        return {
            "ok": bool(acceso),
            "query": raw,
            "acceso": acceso,
            "login_url": got.get("login_url") or "",
            "portal": got.get("portal") or "",
            "click_url": got.get("click_url") or (raw if CLICK_HOST in raw else ""),
            "woid": got.get("woid") or "",
            "matches": [],
            "note": "Link del correo resuelto (payload de AccountLogin).",
        }

    if raw and looks_like_email(raw):
        email = norm_email(raw)
    elif raw and norm_phone(raw) and len(norm_phone(raw)) >= 7:
        phone = norm_phone(raw)

    if not email and not phone:
        return {"ok": False, "error": "Pasa email, teléfono o el link del correo"}

    matches: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def _add(item: dict[str, Any]) -> None:
        key = (item.get("acceso") or "", item.get("email") or "", item.get("phone") or "")
        if key in seen:
            return
        if not any(item.get(k) for k in ("acceso", "portal", "login_url", "click_url", "woid")):
            return
        seen.add(key)
        matches.append(item)

    with _conn() as c:
        if email:
            for r in c.execute(
                "SELECT * FROM safelite_accesos WHERE email=? ORDER BY id DESC",
                (email,),
            ):
                _add(_row_to_item(r))
        if phone:
            for r in c.execute(
                "SELECT * FROM safelite_accesos WHERE phone=? ORDER BY id DESC",
                (phone,),
            ):
                _add(_row_to_item(r))

    for item in _from_leads(email, phone):
        _add(item)
    for item in _from_citas(email, phone):
        _add(item)

    best = next((m for m in matches if m.get("acceso")), None)
    return {
        "ok": bool(best),
        "email": email,
        "phone": phone,
        "acceso": (best or {}).get("acceso") or "",
        "login_url": (best or {}).get("login_url") or "",
        "portal": (best or {}).get("portal") or "",
        "click_url": (best or {}).get("click_url") or "",
        "woid": (best or {}).get("woid") or "",
        "work_order": (best or {}).get("work_order") or "",
        "name": (best or {}).get("name") or "",
        "matches": matches,
        "error": "" if best else "No hay acceso guardado para ese email o número. Pega el link del correo (click.t.safelite.com) con POST.",
    }


def _from_leads(email: str, phone: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with _conn() as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(leads)").fetchall()}
            rows = c.execute("SELECT * FROM leads").fetchall()
    except Exception:
        return out
    for r in rows:
        d = dict(r)
        e = norm_email(d.get("pool_email") or d.get("email") or "")
        p = norm_phone(d.get("pool_phone") or d.get("client_phone") or d.get("phone") or "")
        if email and e != email:
            continue
        if phone and p != phone:
            continue
        if email or phone:
            if not ((email and e == email) or (phone and p == phone)):
                continue
        portal = (d.get("portal") or "").strip()
        if not portal and not (e or p):
            continue
        if not portal:
            continue
        out.append(
            {
                "email": e,
                "phone": p,
                "name": d.get("name") or "",
                "woid": _woid_of(portal),
                "work_order": d.get("work_order") or "",
                "acceso": portal,
                "login_url": "",
                "portal": portal,
                "click_url": "",
                "source": "lead",
                "updated_at": d.get("updated_at") or d.get("booked_at") or "",
            }
        )
    return out


_CITA_KV = re.compile(r"^(Email|Phone|Portal|WOID|WO|Nombre)\s*:\s*(.+)$", re.I)


def parse_citas_text(text: str) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for line in text.splitlines():
        if line.strip().startswith("===="):
            if cur:
                blocks.append(cur)
                cur = {}
            continue
        m = _CITA_KV.match(line.strip())
        if not m:
            continue
        k = m.group(1).lower()
        v = m.group(2).strip()
        if k == "email":
            cur["email"] = v
        elif k == "phone":
            cur["phone"] = v
        elif k == "portal":
            cur["portal"] = v
        elif k == "woid":
            cur["woid"] = v
        elif k == "wo":
            cur["work_order"] = v
        elif k == "nombre":
            cur["name"] = v
    if cur:
        blocks.append(cur)
    return blocks


def _from_citas(email: str, phone: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in (legacy.CITAS, getattr(legacy, "CITAS_DONE", Path())):
        p = Path(path) if path else Path()
        if not p.exists():
            continue
        try:
            blocks = parse_citas_text(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        for b in blocks:
            e = norm_email(b.get("email"))
            ph = norm_phone(b.get("phone"))
            if email or phone:
                if not ((email and e == email) or (phone and ph == phone)):
                    continue
            portal = b.get("portal") or ""
            if not portal:
                continue
            out.append(
                {
                    "email": e,
                    "phone": ph,
                    "name": b.get("name") or "",
                    "woid": b.get("woid") or _woid_of(portal),
                    "work_order": b.get("work_order") or "",
                    "acceso": portal,
                    "login_url": "",
                    "portal": portal,
                    "click_url": "",
                    "source": p.name,
                    "updated_at": "",
                }
            )
    return out


_reindexed = False


def reindex_quiet() -> None:
    global _reindexed
    if _reindexed:
        return
    _reindexed = True
    try:
        reindex()
    except Exception:
        _reindexed = False


def reindex() -> int:
    init_acceso_table()
    n = 0
    for item in _from_leads("", ""):
        if not item.get("portal"):
            continue
        remember(
            email=item.get("email") or "",
            phone=item.get("phone") or "",
            name=item.get("name") or "",
            woid=item.get("woid") or "",
            work_order=item.get("work_order") or "",
            portal=item.get("portal") or "",
            source="lead",
        )
        n += 1
    for item in _from_citas("", ""):
        if not item.get("portal"):
            continue
        if item.get("email") or item.get("phone"):
            remember(
                email=item.get("email") or "",
                phone=item.get("phone") or "",
                name=item.get("name") or "",
                woid=item.get("woid") or "",
                work_order=item.get("work_order") or "",
                portal=item.get("portal") or "",
                source=item.get("source") or "citas",
            )
            n += 1
    return n
