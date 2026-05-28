"""Extractor de estado: actualiza EstadoCapturado a partir del mensaje del usuario.

Usa gpt-4o-mini con structured output. Mantiene los datos del papá (nivel, edad,
escuela, miedos, etc.) actualizados turno a turno para inyectarlos al prompt y
evitar repreguntar.

Estrategia: en cada turno se envía al modelo (a) el estado actual capturado y
(b) el último mensaje, y se le pide que devuelva los campos NUEVOS detectados.
Hacemos merge defensivo (no sobreescribir si el modelo no detectó nada).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from app.adapters.openai_client import get_openai
from app.core.state import EstadoCapturado, HijoInfo, NivelEducativo

log = logging.getLogger(__name__)


class ExtraccionTurno(BaseModel):
    """Output del extractor. Cualquier campo puede ser None si no se detectó."""

    nombre_papa: str | None = None
    email_papa: str | None = None  # D.3 (Lily 2026-05-27): captura pre-cita
    telefono: str | None = None  # D.3: número celular del papá
    nivel_buscado: str | None = None  # 'maternal'|'kinder'|'primaria'|'secundaria'|None
    nombre_hijo: str | None = None
    edad_hijo: int | None = Field(default=None, ge=0, le=20)
    # Fix B.1 (2026-05-19): campo separado para evitar que "tengo 4 hijos"
    # se interprete como "edad_hijo=4". Si el papá dice una cantidad de hijos,
    # va aquí; edad_hijo queda null hasta que se mencione "X años / añitos".
    cantidad_hijos: int | None = Field(default=None, ge=0, le=10)
    grado_hijo: str | None = None
    escuela_actual: str | None = None
    diagnostico_hijo: str | None = None
    miedos_nuevos: list[str] = Field(default_factory=list)
    resono_con_nuevos: list[str] = Field(default_factory=list)
    objeciones_nuevas: list[str] = Field(default_factory=list)
    pidio_costos: bool = False
    vive_fuera_saltillo: bool = False
    quiere_agendar: bool = False


_SYSTEM_PROMPT = """Eres un extractor de información para Sofía, agente de admisiones de Maple Collège.

Recibes:
1. El estado YA CAPTURADO del papá (datos previos en JSON).
2. El último mensaje del papá.

Tu tarea: detectar datos NUEVOS que aparezcan en el mensaje. Si un dato ya está en el estado, NO lo repitas. Si no detectas nada nuevo en algún campo, déjalo como null o lista vacía.

Reglas:
- "nombre_papa": el nombre propio del papá/mamá cuando se presenta. Detecta patrones como: "Me llamo X", "Soy X", "Mi nombre es X", "Hola, soy X", "Habla X", o cuando firma con su nombre al final ("Saludos, X"). Toma SOLO el nombre y apellido(s) (NO incluyas titulos como "Sr.", "Sra.", "Dr."). Si el papá menciona el nombre del HIJO, eso va en "nombre_hijo", NO aquí. Ver ejemplos few-shot abajo.
- "email_papa": email del papá si aparece (formato `algo@dominio.tld`). Captura literal, sin cambiar mayúsculas. Si no aparece, null.
- "telefono": número celular del papá si aparece. Acepta formatos: "8441234567", "844 123 4567", "+52 844 123 4567", "844-123-45-67". Captura solo dígitos + signo +, sin espacios ni guiones (normaliza). Mínimo 10 dígitos. Si no aparece, null.
- "nivel_buscado": SOLO uno de: maternal, kinder, primaria, secundaria. Mapea variantes naturales: "2do de primaria"→primaria, "primero kinder"→kinder, "preescolar"→kinder, "secu"→secundaria, "mater"→maternal.
- "edad_hijo": número entero entre 0 y 20 — SOLO cuando el papá habla explícitamente de **EDAD** (verbo "tener", palabras "años", "añitos", "meses", "cumplió"). Ver reglas de desambiguación abajo.
- "cantidad_hijos": número entero entre 0 y 10 — SOLO cuando el papá menciona **CUÁNTOS HIJOS** tiene (no la edad). Ver reglas de desambiguación abajo.
- "grado_hijo": tal como lo dijo el papá ("2do primaria", "1ro kinder", etc.).
- "diagnostico_hijo": SOLO si el papá menciona explícitamente un diagnóstico (autismo, TDAH, etc.). Si no, null.
- "miedos_nuevos": ej. "bullying", "que no aprenda", "falta de disciplina". Lista corta de etiquetas.
- "resono_con_nuevos": ideas que parecieron resonarle ("le gustó la metodología", "le importó el vínculo").
- "objeciones_nuevas": objeciones explícitas ("está caro", "no tienen tarea", "es muy flexible").
- "pidio_costos": true SOLO si pregunta directamente por precio/costo/colegiatura.
- "vive_fuera_saltillo": true si menciona que no vive en Saltillo o va a mudarse.
- "quiere_agendar": true si pide cita, visita, conocer el colegio explícitamente.

## Desambiguación CRÍTICA: cantidad de hijos vs edad del hijo

Bug detectado en reunión Maple 2026-05-19: el papá decía "tengo cuatro hijos" y Sofía interpretaba que el hijo tenía 4 años. Reglas estrictas:

**Va a `cantidad_hijos` (NO a `edad_hijo`):**
- "tengo N hijos" / "somos N hijos" / "son N (hijos/niños)" / "tengo N niños/niñas"
- "tengo dos niños y una niña" → cantidad_hijos=3
- Cualquier frase donde el número se refiere al CONTEO de hijos, no a años.

**Va a `edad_hijo` (NO a `cantidad_hijos`):**
- "mi hijo tiene N años / añitos / meses"
- "él tiene N años" / "ella tiene N"
- "ya cumplió N" / "N años cumplidos"
- "es de N años" / "tiene N"
- Cualquier frase donde el número se refiere a la EDAD.

**Ambiguo (deja ambos en null — Sofía preguntará):**
- "N" solo, sin verbo ni contexto ("4", "cuatro").
- "X niños" sin verbo de posesión ("muchos niños", "varios").

## Ejemplos few-shot

Mensaje: "tengo cuatro hijos"
Output: {"cantidad_hijos": 4, "edad_hijo": null, ...}

Mensaje: "somos 3 hijos en la familia"
Output: {"cantidad_hijos": 3, "edad_hijo": null, ...}

Mensaje: "tengo 2 niños y 1 niña"
Output: {"cantidad_hijos": 3, "edad_hijo": null, ...}

Mensaje: "mi hijo tiene 4 años"
Output: {"cantidad_hijos": null, "edad_hijo": 4, ...}

Mensaje: "él tiene 4 añitos"
Output: {"cantidad_hijos": null, "edad_hijo": 4, ...}

Mensaje: "ya cumplió 5"
Output: {"cantidad_hijos": null, "edad_hijo": 5, ...}

Mensaje: "es de 4 años"
Output: {"cantidad_hijos": null, "edad_hijo": 4, ...}

Mensaje: "4"
Output: {"cantidad_hijos": null, "edad_hijo": null, ...}

Mensaje: "cuatro"
Output: {"cantidad_hijos": null, "edad_hijo": null, ...}

## Ejemplos few-shot — nombre_papa

Mensaje: "Me llamo Oscar Rodriguez"
Output: {"nombre_papa": "Oscar Rodriguez", ...}

Mensaje: "Soy Ana, busco info para mi hijo"
Output: {"nombre_papa": "Ana", "quiere_agendar": false, ...}

Mensaje: "Hola, soy Juan Carlos Pérez"
Output: {"nombre_papa": "Juan Carlos Pérez", ...}

Mensaje: "Mi nombre es Maria Elena"
Output: {"nombre_papa": "Maria Elena", ...}

Mensaje: "Me llamo Oscar Rodriguez, busco kinder para mi hijo de 5 años"
Output: {"nombre_papa": "Oscar Rodriguez", "nivel_buscado": "kinder", "edad_hijo": 5, ...}

Mensaje: "habla la mamá de Lucía"
Output: {"nombre_papa": null, "nombre_hijo": "Lucía", ...}

Mensaje: "mi hijo Diego está en 2do de primaria"
Output: {"nombre_papa": null, "nombre_hijo": "Diego", "grado_hijo": "2do de primaria", "nivel_buscado": "primaria", ...}

Mensaje: "Hola"
Output: {"nombre_papa": null, ...}

## Ejemplos few-shot — email_papa y telefono (D.3 — Lily 2026-05-27)

Mensaje: "Mi correo es oscar@example.com"
Output: {"email_papa": "oscar@example.com", ...}

Mensaje: "Soy Oscar, mi número es 8441234567"
Output: {"nombre_papa": "Oscar", "telefono": "8441234567", ...}

Mensaje: "Mi celular es +52 844 123 4567 y mi correo ana.perez@gmail.com"
Output: {"telefono": "+528441234567", "email_papa": "ana.perez@gmail.com", ...}

Mensaje: "844-123-45-67"
Output: {"telefono": "8441234567", ...}

Mensaje: "te paso mi info: María López, 844 555 1212, maria@correo.mx"
Output: {"nombre_papa": "María López", "telefono": "8445551212", "email_papa": "maria@correo.mx", ...}

Devuelve EXCLUSIVAMENTE JSON con la estructura de ExtraccionTurno.
"""


async def extraer_de_mensaje(
    mensaje: str,
    estado_actual: EstadoCapturado,
) -> ExtraccionTurno:
    """Extrae datos nuevos del último mensaje del papá.

    No mergea — eso lo hace el caller con `aplicar_extraccion()`.
    """
    openai = get_openai()
    if not openai.is_configured():
        log.warning("openai not configured, skipping extraction")
        return ExtraccionTurno()

    estado_json = estado_actual.model_dump_json(exclude_defaults=True)
    user_text = (
        f"ESTADO YA CAPTURADO:\n{estado_json}\n\n"
        f"ÚLTIMO MENSAJE DEL PAPÁ:\n{mensaje}\n\n"
        f"Detecta SOLO datos NUEVOS que no estén ya en el estado."
    )

    try:
        raw = await openai.classify(text=user_text, instructions=_SYSTEM_PROMPT)
    except Exception as exc:
        log.warning("state_extractor api error", extra={"error": str(exc)})
        return ExtraccionTurno()

    return _parse_extraction(raw)


def _parse_extraction(raw: str) -> ExtraccionTurno:
    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data: dict[str, Any] = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        log.warning("state_extractor non-json", extra={"raw": raw[:200], "err": str(exc)})
        return ExtraccionTurno()

    try:
        return ExtraccionTurno.model_validate(data)
    except Exception as exc:
        log.warning("state_extractor invalid schema", extra={"data": data, "err": str(exc)})
        return ExtraccionTurno()


def aplicar_extraccion(
    estado_actual: EstadoCapturado,
    extraccion: ExtraccionTurno,
) -> EstadoCapturado:
    """Aplica los datos nuevos al estado existente (merge defensivo).

    Reglas de merge:
    - Strings nuevos sobrescriben SOLO si el actual es None.
    - Booleans true se "sticky" — no se reescriben a false.
    - Listas se acumulan (sin duplicados).
    - Si aparece nivel/nombre/edad de hijo, se agrega o actualiza HijoInfo.
    """
    nuevo = estado_actual.model_copy(deep=True)

    if extraccion.nombre_papa and not nuevo.nombre_papa:
        nuevo.nombre_papa = extraccion.nombre_papa

    if extraccion.email_papa and not nuevo.email_papa:
        nuevo.email_papa = extraccion.email_papa

    if extraccion.telefono and not nuevo.telefono:
        nuevo.telefono = extraccion.telefono

    if extraccion.pidio_costos:
        nuevo.pidio_costos = True

    if extraccion.vive_fuera_saltillo:
        nuevo.vive_fuera_saltillo = True

    # Acumular listas con dedup preservando orden
    for miedo in extraccion.miedos_nuevos:
        if miedo and miedo not in nuevo.miedos:
            nuevo.miedos.append(miedo)

    for resono in extraccion.resono_con_nuevos:
        if resono and resono not in nuevo.resono_con:
            nuevo.resono_con.append(resono)

    for obj in extraccion.objeciones_nuevas:
        if obj and obj not in nuevo.objeciones_planteadas:
            nuevo.objeciones_planteadas.append(obj)

    # Actualizar/crear info de hijo si el extractor detectó algo
    if extraccion.nivel_buscado:
        try:
            nivel_enum = NivelEducativo(extraccion.nivel_buscado.lower())
        except ValueError:
            nivel_enum = None
        if nivel_enum:
            nuevo.nivel_buscado_actual = nivel_enum
            # Sincroniza con el (primer) hijo si no hay info
            _upsert_hijo(
                nuevo,
                nivel=nivel_enum,
                nombre=extraccion.nombre_hijo,
                edad=extraccion.edad_hijo,
                grado=extraccion.grado_hijo,
                escuela_actual=extraccion.escuela_actual,
                diagnostico=extraccion.diagnostico_hijo,
            )
    elif (
        extraccion.nombre_hijo
        or extraccion.edad_hijo is not None
        or extraccion.grado_hijo
        or extraccion.escuela_actual
        or extraccion.diagnostico_hijo
    ):
        _upsert_hijo(
            nuevo,
            nivel=None,
            nombre=extraccion.nombre_hijo,
            edad=extraccion.edad_hijo,
            grado=extraccion.grado_hijo,
            escuela_actual=extraccion.escuela_actual,
            diagnostico=extraccion.diagnostico_hijo,
        )

    return nuevo


def _upsert_hijo(
    estado: EstadoCapturado,
    *,
    nivel: NivelEducativo | None,
    nombre: str | None,
    edad: int | None,
    grado: str | None,
    escuela_actual: str | None,
    diagnostico: str | None,
) -> None:
    """Actualiza el primer hijo cuyo nivel coincida, o crea uno nuevo."""
    target: HijoInfo | None = None
    if nivel is not None:
        for h in estado.hijos:
            if h.nivel == nivel:
                target = h
                break
    if target is None and estado.hijos and nivel is None:
        target = estado.hijos[0]
    if target is None:
        target = HijoInfo(nivel=nivel)
        estado.hijos.append(target)

    if nivel and not target.nivel:
        target.nivel = nivel
    if nombre and not target.nombre:
        target.nombre = nombre
    if edad is not None and target.edad is None:
        target.edad = edad
    if grado and not target.grado:
        target.grado = grado
    if escuela_actual and not target.escuela_actual:
        target.escuela_actual = escuela_actual
    if diagnostico and not target.diagnostico:
        target.diagnostico = diagnostico
