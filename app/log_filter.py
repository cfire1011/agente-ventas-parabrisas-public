"""
Filtro de logging que enmascara secretos.

app.log tiene el PAGE_ACCESS_TOKEN en texto plano (llega ahi porque va en la
query string de las URLs de la Graph API y algunas librerias loguean la URL
completa). Cualquiera con acceso al archivo puede publicar como la pagina.

Este filtro no reemplaza la rotacion del token: el que ya se escribio sigue
comprometido y hay que rotarlo en Meta.
"""
from __future__ import annotations

import logging
import os
import re

MASCARA = "***"

# token de pagina de Meta en query string o cuerpo
_PATRONES = [
    re.compile(r"(?i)(access_token=)[A-Za-z0-9_\-\.]+"),
    re.compile(r"(?i)(\"access_token\"\s*:\s*\")[^\"]+"),
    re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?)[A-Za-z0-9_\-\.]{12,}"),
    re.compile(r"(?i)((?:api[_-]?key|apikey|secret|token|password|passwd)[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9_\-\.]{8,}"),
    # tokens de Meta: siempre empiezan con EAA
    re.compile(r"\bEAA[A-Za-z0-9]{10,}"),
    # llaves de Groq / OpenAI
    re.compile(r"\b(?:gsk|sk)_[A-Za-z0-9]{10,}"),
]

# variables de entorno con secretos: se enmascara su valor literal
_ENV_SECRETAS = (
    "PAGE_ACCESS_TOKEN", "VERIFY_TOKEN", "APP_SECRET", "GROQ_API_KEY",
    "OPENAI_API_KEY", "SAFELITE_TOKEN", "ADMIN_TOKEN",
)


def _valores_secretos() -> list[str]:
    out = []
    for k in _ENV_SECRETAS:
        v = (os.getenv(k) or "").strip()
        if len(v) >= 8:
            out.append(v)
    return sorted(out, key=len, reverse=True)


def enmascarar(texto: str) -> str:
    """Devuelve el texto sin secretos."""
    if not texto:
        return texto
    for v in _valores_secretos():
        texto = texto.replace(v, MASCARA)
    for rx in _PATRONES:
        if rx.groups:
            texto = rx.sub(lambda m: m.group(1) + MASCARA, texto)
        else:
            texto = rx.sub(MASCARA, texto)
    # evita "******" cuando dos patrones tocan lo mismo
    return re.sub(r"(?:" + re.escape(MASCARA) + r"){2,}", MASCARA, texto)


class FiltroSecretos(logging.Filter):
    """Enmascara secretos en el mensaje y en los args del record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = enmascarar(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        k: enmascarar(v) if isinstance(v, str) else v
                        for k, v in record.args.items()
                    }
                else:
                    record.args = tuple(
                        enmascarar(a) if isinstance(a, str) else a
                        for a in record.args
                    )
        except Exception:
            # un fallo enmascarando nunca debe tumbar el logging
            pass
        return True


def instalar() -> None:
    """Aplica el filtro al root y a todos los handlers ya montados."""
    filtro = FiltroSecretos()
    root = logging.getLogger()
    if not any(isinstance(f, FiltroSecretos) for f in root.filters):
        root.addFilter(filtro)
    for h in root.handlers:
        if not any(isinstance(f, FiltroSecretos) for f in h.filters):
            h.addFilter(filtro)
    # las librerias que loguean URLs completas son las que filtraron el token
    for nombre in ("urllib3", "requests", "httpx", "httpcore", "uvicorn.access"):
        lg = logging.getLogger(nombre)
        if not any(isinstance(f, FiltroSecretos) for f in lg.filters):
            lg.addFilter(filtro)
