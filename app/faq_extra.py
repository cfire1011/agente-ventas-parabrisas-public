"""
Preguntas frecuentes que hoy caen en 'chat' generico.

Mirando las conversaciones reales de los 22 leads que nunca dieron el auto, el
patron se repite: el cliente pregunta algo legitimo ("¿donde estan ubicados?",
"¿que garantia tienen?", "¿no vienen a domicilio?", "dame tu telefono") y el bot
contesta "Claro, ¿que carro tienes? Año, marca y modelo." — la misma frase,
palabra por palabra, hasta que el cliente se va.

Un vendedor humano responde la pregunta Y reencauza. Aca solo se DETECTA la
intencion; la redaccion vive en legacy para poder reusar idioma, precio y el
reencauce segun lo que falte (auto -> zip -> horarios).
"""
from __future__ import annotations

import re
import unicodedata


def _norm(text: str | None) -> str:
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", s).strip()


# El orden importa: la primera que matchea gana.
_INTENCIONES: list[tuple[str, str]] = [
    # Marketplace / anuncio: el drop-off #1 en chats reales.
    ("negocio",
     r"\b(informaci[oó]n del negocio|mas informaci[oó]n del negocio|"
     r"sobre el anuncio|algo m[aá]s sobre el anuncio|"
     r"cu[eé]ntenme del negocio|info del negocio|"
     r"more information about (the )?(business|ad|listing)|"
     r"tell me more about (the )?(business|ad|listing)|"
     r"can you tell me more)\b"),

    # quiere chatear con alguien: reencauzar al flujo de cotización
    ("humano",
     r"\b(hay\s+alguien\s+con\s+quien|"
     r"alguien\s+con\s+quien\s+(pueda\s+)?chatear|"
     r"puedo\s+chatear|hablar\s+con\s+(alguien|una\s+persona|un\s+agente)|"
     r"quiero\s+hablar\s+con|"
     r"is\s+there\s+someone\s+i\s+can\s+(chat|talk))\b"),

    # ¿puedo hablar con alguien? — 2º drop-off real (piden NUESTRO numero)
    ("telefono",
     r"\b(tu\s+(numero|telefono)|su\s+(numero|telefono)|numero\s+de\s+(telefono|contacto)|"
     r"me\s+(pasas|das)\s+(un|el|tu)\s+(numero|telefono)|"
     r"puedo\s+llamar|los\s+puedo\s+llamar|hay\s+alguien\s+que\s+(me\s+)?llame|"
     r"phone\s+number|call\s+(you|me))\b"),

    # ¿donde estan? / ¿llegan a mi ciudad? — la mas frecuente
    #
    # Nestor Osorio: "vivo en Boise no se si tiene servicio aca" nunca
    # matcheaba porque el patron viejo solo aceptaba "tienen" (plural) y
    # exigia que el verbo fuera SEGUIDO directo por la palabra de lugar
    # ("tienen aqui"), sin permitir "servicio" en medio ("tiene servicio
    # aca"). El cliente se quedaba sin la respuesta de "damos servicio en
    # los 50 estados" que ya existe en legacy._faq_extra_reply.
    ("ubicacion",
     r"\b(donde\s+(estan|queda|esta|se\s+encuentran|los\s+ubico)|"
     r"que\s+(lugar|logaritmo|logrithmo|sitio|zona)\s+(estan|queda|es)|"
     r"cual\s+es\s+(su|la)\s+(direccion|ubicacion)|"
     r"no\s+se\s+si\s+(tiene[n]?|hay)\s+servicio|"
     r"(estan|estas|esta|hay|tiene|tienen|llegan|van|vienen|cubren|dan)\s+"
     r"(servicio\s+)?(aqui|alli|alla|por|en|hasta|hacia|cerca)\b|"
     r"tienen\s+(local|taller|tienda|sucursal)|"
     r"where\s+(are|r)\s+(you|u)\b|your\s+(location|address)|"
     r"do\s+you\s+(come|serve|cover)\b)"),

    # ¿van a domicilio? / ¿tengo que llevarlo?
    ("domicilio",
     r"\b(a\s+domicilio|van\s+a\s+la\s+casa|vienen\s+a\s+(mi\s+)?(casa|domicilio|trabajo)|"
     r"tengo\s+que\s+(ir|llevar|llevarlo)|hay\s+que\s+llevar|"
     r"lo\s+llevo|se\s+lo\s+llevo|mobile\s+service|do\s+you\s+come\s+to\s+me)\b"),

    # cuanto tarda el trabajo
    ("duracion",
     r"\b(cuanto\s+(tarda|dura|se\s+tarda|demora|tiempo\s+(toma|lleva|tarda))|"
     r"en\s+cuanto\s+tiempo|how\s+long\s+(does|it|will))\b"),

    # ¿lo cubre el seguro?
    ("seguro",
     r"\b(seguro|aseguranza|insurance|deducible|deductible|claim)\b"),

    # ¿lo pueden reparar en vez de cambiar?
    ("reparar",
     r"\b(reparar|reparacion|se\s+puede\s+arreglar|arreglar\s+(el\s+)?(chip|griet\w*|rajadur\w*)|"
     r"chip|rajadur\w*|griet\w*|fisur\w*|repair\s+(it|the\s+chip)|"
     r"solo\s+(un|una)\s+(chip|rajadura|grieta))\b"),

    # ¿es original? ¿que marca de vidrio?
    ("calidad",
     r"\b(original|oem|calidad|que\s+marca\s+de\s+(vidrio|cristal)|"
     r"es\s+nuevo|usado|generico)\b"),

    # ¿que necesitan de mi?
    ("requisitos",
     r"\b(que\s+(necesitan|necesito|se\s+necesita|hace\s+falta|piden)|"
     r"what\s+do\s+you\s+need)\b"),
]

_COMPILADAS = [(k, re.compile(p)) for k, p in _INTENCIONES]


def detectar(text: str | None) -> str | None:
    """Devuelve la clave de intencion o None."""
    t = _norm(text)
    if not t:
        return None
    for clave, rx in _COMPILADAS:
        if rx.search(t):
            return clave
    return None
