from __future__ import annotations

import os

from fastapi import APIRouter, BackgroundTasks, Header, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from . import legacy
from . import safelite_acceso

router = APIRouter()


@router.get("/")
def root():
    return legacy.root()


@router.get("/dashboard")
def dashboard_page():
    return legacy.root()


@router.get("/privacy", response_class=HTMLResponse)
def privacy_page():
    return legacy.privacy_page()


@router.get("/terms", response_class=HTMLResponse)
def terms_page():
    return legacy.terms_page()


@router.post("/data-deletion")
async def data_deletion(request: Request):
    return await legacy.data_deletion(request)


@router.get("/data-deletion/status")
def data_deletion_status(code: str = ""):
    return legacy.data_deletion_status(code)


@router.post("/api/dashboard/login")
async def dash_login(request: Request):
    return await legacy.dash_login(request)


@router.get("/api/dashboard/me")
def dash_me(x_dashboard_token: str | None = Header(None)):
    return legacy.dash_me(x_dashboard_token)


@router.get("/api/dashboard/leads")
def dash_leads(x_dashboard_token: str | None = Header(None)):
    return legacy.dash_leads(x_dashboard_token)


@router.get("/api/dashboard/leads/{psid}")
def dash_lead_detail(psid: str, x_dashboard_token: str | None = Header(None)):
    return legacy.dash_lead_detail(psid, x_dashboard_token)


@router.patch("/api/dashboard/leads/{psid}")
async def dash_lead_update(psid: str, request: Request, x_dashboard_token: str | None = Header(None)):
    return await legacy.dash_lead_update(psid, request, x_dashboard_token)


@router.post("/api/dashboard/leads/{psid}/finalize")
async def dash_lead_finalize(psid: str, x_dashboard_token: str | None = Header(None)):
    return await legacy.dash_lead_finalize(psid, x_dashboard_token)


@router.get("/api/dashboard/agenda")
def dash_agenda(x_dashboard_token: str | None = Header(None)):
    return legacy.dash_agenda(x_dashboard_token)


@router.get("/api/dashboard/pool")
def dash_pool(x_dashboard_token: str | None = Header(None)):
    return legacy.dash_pool(x_dashboard_token)


def _acceso_auth(request: Request, key: str = "", x_dashboard_token: str | None = None) -> bool:
    """Solo admin logueado. El usuario de chat no ve Safelite."""
    tok = (key or x_dashboard_token or request.headers.get("x-api-key") or "").strip()
    if not tok:
        return False
    try:
        sess = legacy._require_dash(tok, admin=True)
        return sess.get("role") == "admin"
    except Exception:
        return False


@router.get("/api/safelite/acceso")
@router.get("/api/dashboard/safelite/acceso")
def safelite_acceso_get(
    request: Request,
    email: str = "",
    phone: str = "",
    q: str = "",
    key: str = "",
    x_dashboard_token: str | None = Header(None),
):
    if not _acceso_auth(request, key, x_dashboard_token):
        return JSONResponse({"ok": False, "error": "key requerida"}, status_code=401)
    return safelite_acceso.lookup(email=email, phone=phone, q=q)


@router.post("/api/safelite/acceso")
@router.post("/api/dashboard/safelite/acceso")
async def safelite_acceso_post(
    request: Request,
    key: str = "",
    x_dashboard_token: str | None = Header(None),
):
    if not _acceso_auth(request, key, x_dashboard_token):
        return JSONResponse({"ok": False, "error": "key requerida"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    url = str(body.get("url") or body.get("link") or body.get("q") or "").strip()
    email = str(body.get("email") or "").strip()
    phone = str(body.get("phone") or body.get("numero") or "").strip()
    if url and not email and not phone:
        got = safelite_acceso.unwrap_click(url)
        return {
            "ok": True,
            "unwrapped": True,
            "acceso": got.get("login_url") or got.get("portal") or url,
            **got,
        }
    saved = safelite_acceso.remember(
        email=email,
        phone=phone,
        name=str(body.get("name") or ""),
        portal=str(body.get("portal") or ""),
        click_url=url,
        login_url=str(body.get("login_url") or ""),
        woid=str(body.get("woid") or ""),
        work_order=str(body.get("work_order") or ""),
        source="manual",
    )
    found = safelite_acceso.lookup(email=email, phone=phone, q=url)
    found["saved"] = saved
    return found


@router.get("/acceso")
def acceso_page():
    return {
        "ok": True,
        "mode": "api",
        "lookup": "GET /api/safelite/acceso?q=",
        "save": "POST /api/safelite/acceso",
    }


@router.post("/api/dashboard/leads/{psid}/message")
async def dash_lead_message(psid: str, request: Request, x_dashboard_token: str | None = Header(None)):
    return await legacy.dash_lead_message(psid, request, x_dashboard_token)


@router.get("/api/dashboard/settings")
def dash_settings_get(x_dashboard_token: str | None = Header(None)):
    return legacy.dash_settings_get(x_dashboard_token)


@router.post("/api/dashboard/settings")
async def dash_settings_set(request: Request, x_dashboard_token: str | None = Header(None)):
    return await legacy.dash_settings_set(request, x_dashboard_token)


@router.get("/api/dashboard/citas")
def dash_citas(x_dashboard_token: str | None = Header(None)):
    return legacy.dash_citas(x_dashboard_token)


def _lead_auto(l: dict) -> str:
    return " ".join(x for x in [l.get("year") or "", l.get("make") or "", l.get("model") or ""] if x).strip()


def _has_vehicle(l: dict) -> bool:
    return bool(l.get("year") and l.get("make") and l.get("model"))


def _muted(l: dict) -> bool:
    return legacy.is_bot_muted(l)


def build_insights() -> dict:
    leads = legacy.list_leads()
    total = len(leads)
    booked = [l for l in leads if l.get("work_order") or (l.get("stage") or "") in ("ganado", "finalizado")]
    perdido = [l for l in leads if (l.get("stage") or "") == "perdido"]
    muted = [l for l in leads if _muted(l) and (l.get("stage") or "") not in ("finalizado", "ganado", "perdido")]
    no_auto = [l for l in leads if not _has_vehicle(l) and (l.get("stage") or "") not in ("finalizado", "perdido")]
    no_zip = [l for l in leads if _has_vehicle(l) and not (l.get("zip") or "").strip() and (l.get("stage") or "") not in ("finalizado", "perdido")]
    veh_fail = [
        l for l in leads
        if str(l.get("slots_error") or "") in (
            "vehicle_fail", "vehicle_ambiguo", "vehicle_api_error", "esperando_modelo", "sin_auto"
        )
        and (l.get("stage") or "") not in ("finalizado", "perdido")
    ]
    falta_tel = [
        l for l in leads
        if (l.get("slot_date") or l.get("slots_json"))
        and not (l.get("client_phone") or l.get("phone"))
        and (l.get("stage") or "") not in ("finalizado", "perdido")
    ]
    from .nudge import horarios_enviados
    falta_hora = [
        l for l in leads
        if _has_vehicle(l)
        and not horarios_enviados(l)
        and (l.get("stage") or "") not in ("finalizado", "perdido", "ganado")
        and "humano_inbox" not in str(l.get("slots_error") or "")
    ]
    recordados = [l for l in leads if (l.get("nudge_sent") or "").strip()]
    humanos = [
        l for l in leads
        if (l.get("stage") or "") in ("agente", "handoff", "humano", "humano_pendiente", "cerrando")
        or (_muted(l) and l.get("slot_date"))
    ]

    def card(l: dict, why: str) -> dict:
        return {
            "psid": l.get("psid"),
            "name": l.get("name") or "Sin nombre",
            "phone": l.get("client_phone") or l.get("phone") or "",
            "auto": _lead_auto(l) or "—",
            "zip": l.get("zip") or "",
            "stage": l.get("stage") or "nuevo",
            "muted": _muted(l),
            "slots_error": l.get("slots_error") or "",
            "slot_date": l.get("slot_date") or "",
            "why": why,
            "updated_at": l.get("updated_at") or "",
        }

    errors: dict[str, int] = {}
    for l in leads:
        err = (l.get("slots_error") or "").strip()
        if err:
            errors[err] = errors.get(err, 0) + 1

    return {
        "stats": {
            "total": total,
            "booked": len(booked),
            "perdido": len(perdido),
            "conversion": round((len(booked) / total) * 100) if total else 0,
            "muted": len(muted),
            "no_auto": len(no_auto),
            "no_zip": len(no_zip),
            "veh_fail": len(veh_fail),
            "falta_tel": len(falta_tel),
            "falta_hora": len(falta_hora),
            "recordados": len(recordados),
            "humanos": len(humanos),
        },
        "errors": errors,
        "accion": {
            "despertar": [card(l, "bot apagado — no le responde") for l in muted[:40]],
            "modelo": [card(l, "Safelite no traga el auto") for l in veh_fail[:40]],
            "falta_auto": [card(l, "falta año / marca / modelo") for l in no_auto[:40]],
            "falta_zip": [card(l, "tiene auto, falta zip") for l in no_zip[:40]],
            "falta_tel": [card(l, "vio horarios, no hay teléfono") for l in falta_tel[:40]],
            "falta_hora": [
                card(l, "aún no le mandamos horarios — recordatorio a los 2 días si no contesta")
                for l in falta_hora[:40]
            ],
            "recordados": [
                card(l, "ya se le mandó recordatorio: " + str(l.get("nudge_sent") or ""))
                for l in recordados[:40]
            ],
            "cerrar": [card(l, "te toca a ti — eligió día o está muteado") for l in humanos[:40]],
        },
    }


@router.get("/api/dashboard/insights")
def dash_insights(x_dashboard_token: str | None = Header(None)):
    legacy._require_dash(x_dashboard_token, admin=True)
    return build_insights()


@router.get("/live")
def live_page():
    return {
        "ok": True,
        "mode": "api",
        "live": "GET /api/dashboard/fb/live",
        "sync": "POST /api/dashboard/fb/sync",
        "thread": "GET /api/dashboard/fb/thread/{psid}",
    }


@router.get("/api/dashboard/fb/live")
def dash_fb_live(x_dashboard_token: str | None = Header(None)):
    legacy._require_dash(x_dashboard_token, admin=True)
    from .fb_inbox import live_stats
    return live_stats()


@router.post("/api/dashboard/fb/sync")
def dash_fb_sync(x_dashboard_token: str | None = Header(None)):
    legacy._require_dash(x_dashboard_token, admin=True)
    from .fb_inbox import sync_from_graph
    try:
        return sync_from_graph()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:240]}, status_code=502)


@router.get("/api/dashboard/fb/thread/{psid}")
def dash_fb_thread(psid: str, x_dashboard_token: str | None = Header(None)):
    legacy._require_dash(x_dashboard_token, admin=True)
    from .fb_inbox import conversation_thread
    return {"ok": True, "psid": psid, "messages": conversation_thread(psid)}


@router.post("/api/dashboard/leads/{psid}/unmute")
def dash_unmute(psid: str, x_dashboard_token: str | None = Header(None)):
    legacy._require_dash(x_dashboard_token)
    lead = legacy.get_lead(psid)
    if not lead:
        return JSONResponse({"ok": False, "error": "Lead no encontrado"}, status_code=404)
    st = (lead.get("stage") or "").lower()
    if st in ("agente", "handoff", "humano", "humano_pendiente"):
        st = "calificando"
    legacy.upsert_lead(psid, bot_muted="", stage=st)
    try:
        from .aprender import rec
        rec("UNMUTE", psid, "dashboard", lead)
    except Exception:
        pass
    return {"ok": True, "lead": legacy.get_lead(psid)}


@router.post("/api/dashboard/leads/{psid}/hora")
def dash_hora(psid: str, x_dashboard_token: str | None = Header(None)):
    legacy._require_dash(x_dashboard_token)
    if not legacy.get_lead(psid):
        return JSONResponse({"ok": False, "error": "Lead no encontrado"}, status_code=404)
    from .flow import force_send_slots
    reply = force_send_slots(psid)
    if not reply:
        return JSONResponse({"ok": False, "error": "Falta auto o zip"}, status_code=400)
    ok = legacy.send_msg(psid, reply)
    try:
        from .aprender import rec
        rec("HORA", psid, "ok" if ok else "send_fail", legacy.get_lead(psid))
    except Exception:
        pass
    return {"ok": bool(ok), "reply": reply}
