"""Tools determinísticos invocables por el orchestrator.

Cada tool es una función async pura: recibe parámetros tipados, hace su query
(Supabase, OpenAI, Calendar, Channel), devuelve un dataclass tipado. NO mutan
el estado de la conversación.

Tools disponibles:
- `kb_search` — retrieval semántico contra `documents_maple` (Supabase pgvector)
- `precios` — query de tabla `precios_por_nivel` por nivel + sub_nivel
- `horarios` — query de tabla `horarios_por_nivel`
- `campus` — query de tabla `campus`
- `becas` — query de tabla `becas`
- `send_image` — envío de imagen vía Channel
- `send_sticker` — envío de sticker vía Channel
- `calendar` — agendar evento en Google Calendar (Bloque 4 placeholder)
"""

from app.tools.becas import get_becas
from app.tools.calendar import CalendarTool, get_calendar_tool
from app.tools.campus import get_campus_para_nivel
from app.tools.horarios import get_horario
from app.tools.kb_search import KbResult, kb_search
from app.tools.precios import PrecioResult, get_precio
from app.tools.send_image import enviar_imagen_costos_kinder
from app.tools.send_sticker import enviar_sticker_despedida

__all__ = [
    "CalendarTool",
    "KbResult",
    "PrecioResult",
    "enviar_imagen_costos_kinder",
    "enviar_sticker_despedida",
    "get_becas",
    "get_calendar_tool",
    "get_campus_para_nivel",
    "get_horario",
    "get_precio",
    "kb_search",
]
