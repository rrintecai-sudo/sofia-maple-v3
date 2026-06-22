"""Modelos de estado de la conversación.

EstadoCapturado = los datos del papá que ya conocemos (anti-pregunta-repetida).
EstadoConversacion = estado completo de la sesión (canal, fase, modo, frases usadas, etc.).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Canal(StrEnum):
    """Canales soportados por Sofía."""

    WHATSAPP = "whatsapp"
    TELEGRAM = "telegram"
    WEB = "web"


class FaseJourney(StrEnum):
    """Fases del journey conversacional definidas en el system prompt v2.8."""

    BIENVENIDA = "bienvenida"
    DESCUBRIMIENTO = "descubrimiento"
    EDUCACION = "educacion"
    INFORMACION = "informacion"
    OBJECIONES = "objeciones"
    AGENDADO = "agendado"
    POST_AGENDADO = "post_agendado"


class Modo(StrEnum):
    """Modos de operación de Sofía."""

    NORMAL = "normal"
    APRENDIZAJE = "aprendizaje"


class NivelEducativo(StrEnum):
    """Niveles educativos de Maple Collège."""

    MATERNAL = "maternal"
    KINDER = "kinder"
    PRIMARIA = "primaria"
    SECUNDARIA = "secundaria"


class FaseAgendado(StrEnum):
    """Fase PEGAJOSA del sub-flujo de agendado (PASO 1, 2026-05-29).

    Independiente de `FaseJourney` (que se recalcula por intent cada turno).
    Esta máquina la controla el CÓDIGO, no Haiku ni el clasificador:

    - EXPLORANDO: conversación normal; aún no hay señal de querer agendar.
    - AGENDANDO: se entró con la primera señal (intent QUIERE_AGENDAR o
      expresión temporal). NO se reevalúa a la baja turno a turno — el código
      colecta los 6 datos + día/hora en los slots hasta tenerlos todos.
    - CERRADO: la cita se creó (appointment_id existe). No se reabre.
    """

    EXPLORANDO = "explorando"
    AGENDANDO = "agendando"
    CERRADO = "cerrado"


class ClasificacionLead(StrEnum):
    """Clasificación interna del prospecto."""

    CALIFICADO = "calificado"  # 🟢
    POTENCIAL = "potencial"  # 🟡
    NO_COMPATIBLE = "no_compatible"  # 🔴
    SIN_CLASIFICAR = "sin_clasificar"


class HijoInfo(BaseModel):
    """Info de un hijo del papá. Si hay varios, se mantienen como lista."""

    nombre: str | None = None
    edad: int | None = Field(default=None, ge=0, le=20)
    # Edad en MESES — solo maternal, para distinguir la modalidad (Infants 18-24m vs
    # Baby 12-18m, que con la edad en años se confunden). Capturada determinísticamente.
    edad_meses: int | None = Field(default=None, ge=0, le=240)
    nivel: NivelEducativo | None = None
    grado: str | None = None  # "2° primaria", "3° kinder", etc.
    escuela_actual: str | None = None
    diagnostico: str | None = None  # 'autismo', 'tdah', neurodivergente; sólo dato operativo


class EstadoCapturado(BaseModel):
    """Datos del papá que ya conocemos. Se inyecta al prompt para evitar repreguntas.

    Cada campo se va llenando turno a turno por el extractor (Bloque 3).
    """

    model_config = ConfigDict(extra="ignore")

    nombre_papa: str | None = None
    telefono: str | None = None
    email_papa: str | None = None  # D.3 (Lily 2026-05-27): requerido antes de agendar
    hijos: list[HijoInfo] = Field(default_factory=list)
    nivel_buscado_actual: NivelEducativo | None = None  # el nivel del que se habla ahora
    presupuesto_mencionado: bool = False
    pidio_costos: bool = False
    costos_compartidos_niveles: list[NivelEducativo] = Field(default_factory=list)
    miedos: list[str] = Field(default_factory=list)
    resono_con: list[str] = Field(default_factory=list)
    objeciones_planteadas: list[str] = Field(default_factory=list)
    cita_agendada: bool = False
    fecha_cita: datetime | None = None
    campus_cita: Literal["Campus 1", "Campus 2"] | None = None

    # PASO 1 (2026-05-29) — máquina de agendado controlada por código.
    # fase_agendado es PEGAJOSA; los slots de fecha/hora persisten entre turnos
    # para colectar la cita de forma fragmentada (el papá da el día en un turno
    # y la hora/datos en otros). Todo vive en el JSONB estado_capturado → sin
    # migración de esquema.
    fase_agendado: FaseAgendado = FaseAgendado.EXPLORANDO
    cita_fecha_slot: str | None = None  # 'YYYY-MM-DD' resuelto por código
    cita_hora_slot: str | None = None  # 'HH:MM' (24h) resuelto por código
    # Fechas CONCRETAS (ISO) que el código le propuso al papá en el paso del día.
    # El siguiente turno matchea su respuesta contra estas (elegir_opcion_dia) →
    # ya no hay que parsear "hoy/mañana/el viernes" de forma ambigua.
    opciones_dia_propuestas: list[str] = Field(default_factory=list)
    # Último dato que el CÓDIGO pidió (gate), para capturar la respuesta SUELTA del
    # siguiente turno por su propia vía, sin depender de cómo lo frasee Haiku.
    # Valores: 'nombre_hijo'|'nombre_papa'|'edad'|'correo'|'telefono'|'dia'|'hora'|'grado'.
    ultimo_campo_pedido: str | None = None
    # Bloque B (estrategia): máx UNA pregunta de discovery (sondeo) en TODA la
    # conversación. Una vez hecha, el código recorta las siguientes preguntas de
    # sondeo de Haiku. NO afecta las preguntas de datos del agendado (las emite el
    # código) ni un turno donde el papá pidió algo concreto.
    discovery_pregunta_hecha: bool = False
    # La rama de horarios pidió el grado (primaria/kinder sin grado). El siguiente
    # turno con un grado SUELTO ("3", "1°", "4to") lo resuelve y emite el horario.
    pendiente_grado_horario: bool = False
    # El FUNNEL pidió el grado/edad (nivel sin grado). El siguiente turno con un grado
    # SUELTO lo captura → contenido específico del grado (Gaby/Ceci).
    pendiente_grado_funnel: bool = False
    # Nivel EXACTO sobre el que el funnel pidió el grado. Lo fijamos para que el grado
    # suelto del próximo turno ("primero") se ate a ESE nivel y no a uno viejo que el
    # extractor LLM reponga por contexto (bug: "mejor secundaria"→"primero"→"1° de Kinder").
    pendiente_grado_nivel: str | None = None
    # La rama de COSTOS pidió el grado (primaria sin grado: baja $6,100 vs alta $6,300
    # difieren). El siguiente grado SUELTO lo resuelve y re-emite el PRECIO (no la tabla
    # cruda con claves internas 'primaria_baja'). Bug real: fuga de etiqueta interna.
    pendiente_grado_costos: bool = False

    # Flujo de venta de 3 etapas (el CÓDIGO es dueño; Haiku solo redacta).
    # stage_venta: 'enganche' (inicio) → 'valor' (dando bloques de valor) →
    #              'cierre' (en agendado) → 'agendada' (cita creada).
    # turnos_valor: contador de bloques de valor con continuación del papá; al llegar
    # a umbral_empuje el código ordena el empuje. Pausa con preguntas de info nueva.
    stage_venta: str = "enganche"
    turnos_valor: int = 0
    # Ideas/beats de venta YA dichos en la sesión (no repetir). Compartido por el funnel
    # y la pausa de contenido. Cada turno consume 1-2 beats no usados.
    beats_venta_usados: list[str] = Field(default_factory=list)

    handoff_a_lily: bool = False
    fuente_entrada: str | None = None  # 'dm_redes', 'anuncio_whatsapp', 'referido', 'directo'
    vive_fuera_saltillo: bool = False
    clasificacion: ClasificacionLead = ClasificacionLead.SIN_CLASIFICAR

    def conocemos(self, campo: str) -> bool:
        """¿Ya tenemos el dato `campo` capturado?

        Útil para validators anti-pregunta-repetida.
        """
        valor = getattr(self, campo, None)
        if valor is None:
            return False
        if isinstance(valor, (str, list)) and len(valor) == 0:
            return False
        if isinstance(valor, bool):
            return valor  # solo True cuenta como conocido para flags
        return True

    def hijo_efectivo(self) -> HijoInfo | None:
        """Hijo 'efectivo' para el agendado: FUSIONA todos los hijos en uno
        (primer valor no-None por campo), priorizando el que tiene nombre.

        FIX (d) 2026-06-01: la cita es para UN niño. Si una conversación vieja
        contaminada dejó un hijo HUÉRFANO (p.ej. {edad:4} sin nombre) y el niño
        real quedó en otro registro, el gate de 6 datos NO debe mirar `hijos[0]`
        a ciegas: consolida los fragmentos. NO muta la lista persistida.
        """
        if not self.hijos:
            return None

        def _score(h: HijoInfo) -> int:
            llenos = sum(v is not None for v in (h.nombre, h.edad, h.nivel, h.grado))
            return llenos + (2 if h.nombre else 0)  # desempata a favor del que tiene nombre

        base = max(self.hijos, key=_score)
        merged = base.model_copy(deep=True)
        for h in self.hijos:
            if h is base:
                continue
            for campo in ("nombre", "edad", "edad_meses", "nivel", "grado", "escuela_actual", "diagnostico"):
                if getattr(merged, campo) is None and getattr(h, campo) is not None:
                    setattr(merged, campo, getattr(h, campo))
        return merged


class EstadoConversacion(BaseModel):
    """Estado completo de una sesión de Sofía.

    Persiste en tabla `sofia_conversations` (ver `migrations/001_init_schema.sql`).
    """

    model_config = ConfigDict(extra="ignore")

    session_id: str  # 'whatsapp:5218...' | 'telegram:123' | 'web:<uuid>'
    canal: Canal
    identificador: str  # número, chat_id, uuid
    created_at: datetime = Field(default_factory=lambda: datetime.now())
    updated_at: datetime = Field(default_factory=lambda: datetime.now())

    estado_capturado: EstadoCapturado = Field(default_factory=EstadoCapturado)
    frases_usadas: list[str] = Field(default_factory=list)  # anti-repetición de munición
    fase_journey: FaseJourney = FaseJourney.BIENVENIDA
    agendado: bool = False
    fecha_agendado: datetime | None = None
    modo: Modo = Modo.NORMAL
    notas_internas: str | None = None
    tester: bool = False

    @classmethod
    def nueva(cls, session_id: str) -> EstadoConversacion:
        """Crea un estado inicial a partir del session_id (con prefijo de canal)."""
        canal_str, _, identificador = session_id.partition(":")
        try:
            canal = Canal(canal_str)
        except ValueError as exc:
            raise ValueError(
                f"session_id sin prefijo de canal válido: {session_id!r}. "
                f"Esperado uno de: {[c.value for c in Canal]}:..."
            ) from exc

        return cls(
            session_id=session_id,
            canal=canal,
            identificador=identificador,
        )

    def marcar_frase_usada(self, frase: str) -> None:
        """Registra una frase de munición ya usada en este chat."""
        if frase not in self.frases_usadas:
            self.frases_usadas.append(frase)

    def marcar_agendado(self, fecha: datetime, campus: Literal["Campus 1", "Campus 2"]) -> None:
        """Marca la cita como agendada (impide re-empujar)."""
        self.agendado = True
        self.fecha_agendado = fecha
        self.estado_capturado.cita_agendada = True
        self.estado_capturado.fecha_cita = fecha
        self.estado_capturado.campus_cita = campus
        self.fase_journey = FaseJourney.POST_AGENDADO
