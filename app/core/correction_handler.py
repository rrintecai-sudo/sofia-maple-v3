"""Handler para correcciones del papá (Bloque 5.6 PASO 4 — Causa raíz #3).

Cuando el intent classifier marca `Intent.CORRECCION_DEL_PAPA`, este módulo:
1. Llama a GPT-4o-mini con un prompt específico para detectar QUÉ campo se
   está corrigiendo y cuál es el nuevo valor.
2. Devuelve un `CorreccionDetectada` con los campos a SOBREESCRIBIR
   (a diferencia del extractor regular que preserva valores previos).
3. Si el LLM falla, devuelve None (fallback graceful — el orchestrator
   continúa sin aplicar corrección).

El orchestrator también inyecta un hint al prompt principal para que Sofía
reconozca humildemente la corrección antes de seguir el journey.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from app.adapters.openai_client import get_openai
from app.core.state import EstadoCapturado

log = logging.getLogger(__name__)


@dataclass
class CorreccionDetectada:
    """Campos detectados como corrección. Cada campo None = sin cambio."""

    nivel_buscado: str | None = None  # 'maternal'|'kinder'|'primaria'|'secundaria'
    nombre_hijo: str | None = None
    edad_hijo: int | None = None
    grado_hijo: str | None = None
    escuela_actual: str | None = None
    nombre_papa: str | None = None
    # Instrucciones procedimentales: lo que el papá quiere que cambie en el comportamiento
    instruccion_comportamiento: str | None = None
    # Lista de campos a CLEAR (poner a None/false porque el papá dijo "no, no es eso")
    campos_a_limpiar: list[str] = field(default_factory=list)

    @property
    def es_vacia(self) -> bool:
        return all(
            v is None or (isinstance(v, list) and not v)
            for v in (
                self.nivel_buscado,
                self.nombre_hijo,
                self.edad_hijo,
                self.grado_hijo,
                self.escuela_actual,
                self.nombre_papa,
                self.instruccion_comportamiento,
                self.campos_a_limpiar,
            )
        )


_CORRECTION_PROMPT = """Eres un detector de correcciones para Sofía, agente de admisiones de Maple Collège.

El papá te está corrigiendo o aclarando algo. Tu tarea: detectar qué dato concreto
quiere corregir/aclarar y cuál es el valor nuevo correcto.

Recibes:
1. El estado CAPTURADO actual del papá (JSON).
2. El mensaje de corrección.

Devuelve EXCLUSIVAMENTE JSON con esta estructura (todos los campos opcionales):
{
  "nivel_buscado": "maternal" | "kinder" | "primaria" | "secundaria" | null,
  "nombre_hijo": "string" | null,
  "edad_hijo": <int> | null,
  "grado_hijo": "string (ej. '2° primaria')" | null,
  "escuela_actual": "string" | null,
  "nombre_papa": "string" | null,
  "instruccion_comportamiento": "string corto si el papá te da una INSTRUCCIÓN procedimental (ej. 'no preguntes X', 'cuando te diga Y, haz Z')" | null,
  "campos_a_limpiar": ["nivel_buscado", "nombre_hijo", ...]   // si el papá dijo "no, eso no es", para clearear el campo viejo
}

Reglas:
- Solo llena los campos que el papá esté CORRIGIENDO o ACLARANDO explícitamente.
- Si el papá dice "no, eso no era" sobre un campo, agrega ese campo a `campos_a_limpiar` (para borrar el valor viejo).
- Si el papá da una INSTRUCCIÓN procedimental sobre cómo debes responder (no un dato), pónla en `instruccion_comportamiento`.
- Si no detectas ninguna corrección clara, devuelve {} (objeto vacío) — válido.
"""


async def detectar_correccion(mensaje: str, estado: EstadoCapturado) -> CorreccionDetectada | None:
    """Llama a GPT-4o-mini para detectar la corrección. None si falla o si
    el LLM no detecta nada.
    """
    openai = get_openai()
    if not openai.is_configured():
        log.debug("openai not configured, skipping correction detection")
        return None

    estado_json = estado.model_dump_json(exclude_none=True, indent=2)
    user_text = (
        f"ESTADO ACTUAL (lo que Sofía sabe del papá):\n{estado_json}\n\n"
        f"MENSAJE DE CORRECCIÓN:\n{mensaje}\n\n"
        "Detecta qué se está corrigiendo."
    )

    try:
        raw = await openai.classify(text=user_text, instructions=_CORRECTION_PROMPT)
    except Exception as exc:
        log.warning("correction_handler api error", extra={"error": str(exc)})
        return None

    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        log.warning("correction_handler non-json response", extra={"raw": raw[:200]})
        return None
    if not isinstance(data, dict):
        return None

    return CorreccionDetectada(
        nivel_buscado=data.get("nivel_buscado"),
        nombre_hijo=data.get("nombre_hijo"),
        edad_hijo=data.get("edad_hijo"),
        grado_hijo=data.get("grado_hijo"),
        escuela_actual=data.get("escuela_actual"),
        nombre_papa=data.get("nombre_papa"),
        instruccion_comportamiento=data.get("instruccion_comportamiento"),
        campos_a_limpiar=list(data.get("campos_a_limpiar") or []),
    )


def aplicar_correccion(estado: EstadoCapturado, correccion: CorreccionDetectada) -> EstadoCapturado:
    """Aplica la corrección SOBREESCRIBIENDO los campos detectados.

    A diferencia del extractor normal que preserva valores previos, aquí el papá
    nos está diciendo que el valor anterior está mal.

    Devuelve un EstadoCapturado nuevo (no muta el existente).
    """
    from app.core.state import HijoInfo, NivelEducativo

    new = estado.model_copy(deep=True)

    # Limpiar primero los campos pedidos
    for campo in correccion.campos_a_limpiar:
        if campo == "nivel_buscado":
            new.nivel_buscado_actual = None
        elif campo == "nombre_papa":
            new.nombre_papa = None
        elif campo in ("nombre_hijo", "edad_hijo", "grado_hijo", "escuela_actual"):
            # Aplicar a primer hijo si existe
            if new.hijos:
                if campo == "nombre_hijo":
                    new.hijos[0].nombre = None
                elif campo == "edad_hijo":
                    new.hijos[0].edad = None
                elif campo == "grado_hijo":
                    new.hijos[0].grado = None
                elif campo == "escuela_actual":
                    new.hijos[0].escuela_actual = None

    # Sobreescribir con valores nuevos (si los hay)
    if correccion.nivel_buscado:
        try:
            new.nivel_buscado_actual = NivelEducativo(correccion.nivel_buscado.lower())
        except ValueError:
            log.warning("nivel inválido en corrección", extra={"nivel": correccion.nivel_buscado})
    if correccion.nombre_papa:
        new.nombre_papa = correccion.nombre_papa

    # Cambios al hijo: aplicar al primer hijo, creando uno si no existe
    if any(
        [
            correccion.nombre_hijo,
            correccion.edad_hijo,
            correccion.grado_hijo,
            correccion.escuela_actual,
        ]
    ):
        if not new.hijos:
            new.hijos.append(HijoInfo())
        h = new.hijos[0]
        if correccion.nombre_hijo:
            h.nombre = correccion.nombre_hijo
        if correccion.edad_hijo is not None:
            h.edad = correccion.edad_hijo
        if correccion.grado_hijo:
            h.grado = correccion.grado_hijo
        if correccion.escuela_actual:
            h.escuela_actual = correccion.escuela_actual

    return new
