"""Tests de los tools (con respx para httpx)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from app.config import Settings
from app.tools.becas import get_becas
from app.tools.calendar import CalendarTool
from app.tools.campus import get_campus_para_nivel
from app.tools.horarios import get_horario
from app.tools.kb_search import (
    _cosine_similarity,
    _parse_embedding,
    kb_search,
)
from app.tools.niveles import (
    NivelInfo,
    consultar_edades_de_nivel,
    consultar_nivel_por_edad,
    listar_niveles_vigentes,
)
from app.tools.precios import get_precio
from app.tools.send_image import enviar_imagen_costos_kinder
from app.tools.send_sticker import enviar_sticker_despedida


def _supa_settings() -> Settings:
    return Settings(
        supabase_url="https://x.supabase.co",
        supabase_service_key="sk-svc",
        openai_api_key="sk-openai",
    )


# ============================================================
# kb_search
# ============================================================


def test_parse_embedding_list() -> None:
    assert _parse_embedding([0.1, 0.2, 0.3]) == [0.1, 0.2, 0.3]


def test_parse_embedding_string() -> None:
    """Postgres vector llega como string '[0.1, 0.2, ...]'."""
    assert _parse_embedding("[0.1, 0.2, 0.3]") == [0.1, 0.2, 0.3]


def test_parse_embedding_invalid() -> None:
    assert _parse_embedding("not a vector") is None
    assert _parse_embedding(None) is None


def test_cosine_similarity_identical() -> None:
    assert _cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal() -> None:
    assert _cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)


def test_cosine_similarity_zero_vectors() -> None:
    assert _cosine_similarity([0, 0], [1, 1]) == 0.0


@pytest.mark.asyncio
async def test_kb_search_empty_if_supabase_unset() -> None:
    results = await kb_search("hola", settings=Settings())
    assert results == []


@pytest.mark.asyncio
@respx.mock
async def test_kb_search_via_rpc(monkeypatch) -> None:
    """RPC match_documents devuelve filas → se mapean a KbResult."""
    # Mock OpenAI embeddings
    from app.adapters import openai_client

    class FakeOpenAI:
        settings = _supa_settings()

        def is_configured(self) -> bool:
            return True

        async def embed(self, texts, model=None):
            return [[0.1] * 1536]

    monkeypatch.setattr(openai_client, "_singleton", FakeOpenAI())

    respx.post("https://x.supabase.co/rest/v1/rpc/match_documents").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "content": "chunk de prueba",
                    "metadata": {"section": "Test"},
                    "similarity": 0.92,
                },
                {"id": 2, "content": "otro chunk", "metadata": {}, "similarity": 0.85},
            ],
        )
    )

    results = await kb_search("test query", top_k=2, settings=_supa_settings())
    assert len(results) == 2
    assert results[0].content == "chunk de prueba"
    assert results[0].similarity == 0.92
    assert results[0].section == "Test"

    # Restaurar singleton
    monkeypatch.setattr(openai_client, "_singleton", None)


# ============================================================
# precios
# ============================================================


@pytest.mark.asyncio
async def test_get_precio_returns_none_if_no_supabase() -> None:
    result = await get_precio("primaria_baja", settings=Settings())
    assert result is None


@pytest.mark.asyncio
@respx.mock
async def test_get_precio_query() -> None:
    respx.get("https://x.supabase.co/rest/v1/precios_por_nivel").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "nivel": "primaria_baja",
                    "sub_nivel": "1-3",
                    "ciclo_escolar": "2026-2027",
                    "inscripcion": "10900.00",
                    "colegiatura_mensual": "6100.00",
                    "seguro_escolar": "800.00",
                    "seguro_orfandad": "1100.00",
                    "recursos_educativos": "8800.00",
                    "gastos_escolares": "4300.00",
                    "total_gastos_iniciales": "25850.00",
                    "num_colegiaturas": 11,
                    "fecha_limite_pago": "2026-07-15",
                    "vigente": True,
                    "notas": None,
                }
            ],
        )
    )
    result = await get_precio("primaria_baja", settings=_supa_settings())
    assert result is not None
    assert result.colegiatura_mensual == Decimal("6100.00")
    assert result.num_colegiaturas == 11
    assert "6,100" in result.resumen_corto()


@pytest.mark.asyncio
@respx.mock
async def test_get_precio_returns_none_empty() -> None:
    respx.get("https://x.supabase.co/rest/v1/precios_por_nivel").mock(
        return_value=httpx.Response(200, json=[])
    )
    assert await get_precio("inexistente", settings=_supa_settings()) is None


# ============================================================
# horarios
# ============================================================


@pytest.mark.asyncio
@respx.mock
async def test_get_horario_ok() -> None:
    respx.get("https://x.supabase.co/rest/v1/horarios_por_nivel").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "nivel": "primaria_baja",
                    "modalidad": "regular",
                    "hora_inicio": "08:00:00",
                    "hora_fin": "14:30:00",
                    "dias": "L-V",
                    "vigente": True,
                }
            ],
        )
    )
    r = await get_horario("primaria_baja", settings=_supa_settings())
    assert r is not None
    assert r.hora_inicio == "08:00:00"
    assert "08:00" in r.resumen_corto()


# ============================================================
# campus
# ============================================================


@pytest.mark.asyncio
@respx.mock
async def test_get_campus_para_nivel() -> None:
    respx.get("https://x.supabase.co/rest/v1/campus").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "nombre": "Campus 1",
                    "direccion": "José Figueroa Siller 156",
                    "colonia": "Doctores",
                    "ciudad": "Saltillo",
                    "niveles": ["maternal", "kinder", "primaria_baja"],
                    "vigente": True,
                }
            ],
        )
    )
    r = await get_campus_para_nivel("kinder", settings=_supa_settings())
    assert r is not None
    assert r.nombre == "Campus 1"
    assert "Doctores" in r.resumen_corto()


# ============================================================
# niveles_por_edad (Bloque 5.6 PASO 2)
# ============================================================


def _nivel_row(
    *,
    nivel: str = "infants",
    nombre_display: str = "Infants",
    categoria: str = "maternal",
    edad_min_meses: int = 18,
    edad_max_meses: int = 24,
    grados: list[str] | None = None,
    descripcion: str = "Exploración, lenguaje, primeros vínculos sociales.",
    campus: str = "Campus 1",
    confirmado: bool = False,
) -> dict:
    return {
        "nivel": nivel,
        "nombre_display": nombre_display,
        "categoria": categoria,
        "edad_min_meses": edad_min_meses,
        "edad_max_meses": edad_max_meses,
        "grados": grados or [],
        "descripcion": descripcion,
        "campus": campus,
        "confirmado_por_cliente": confirmado,
    }


def test_nivel_info_rango_legible_meses_solo() -> None:
    n = NivelInfo(
        nivel="cubs_baby",
        nombre_display="Cubs Baby",
        categoria="maternal",
        edad_min_meses=3,
        edad_max_meses=11,
        grados=[],
        descripcion=None,
        campus="Campus 1",
    )
    assert n.rango_legible() == "3 a 11 meses"
    assert "Cubs Baby" in n.resumen_corto()


def test_nivel_info_rango_legible_cross_boundary() -> None:
    """Infants: 18 meses a 2 años. Caso clave del bug."""
    n = NivelInfo(
        nivel="infants",
        nombre_display="Infants",
        categoria="maternal",
        edad_min_meses=18,
        edad_max_meses=24,
        grados=[],
        descripcion=None,
        campus="Campus 1",
    )
    assert n.rango_legible() == "18 meses a 2 años"


def test_nivel_info_rango_legible_anos() -> None:
    n = NivelInfo(
        nivel="primaria_baja",
        nombre_display="Primaria baja",
        categoria="primaria",
        edad_min_meses=72,
        edad_max_meses=108,
        grados=["1°", "2°", "3°"],
        descripcion=None,
        campus="Campus 1",
    )
    assert n.rango_legible() == "6 a 9 años"


@pytest.mark.asyncio
@respx.mock
async def test_consultar_nivel_por_edad_infants() -> None:
    """20 meses → Infants (18-24m). Caso anti-bug."""
    respx.get("https://x.supabase.co/rest/v1/niveles_por_edad").mock(
        return_value=httpx.Response(200, json=[_nivel_row()])
    )
    r = await consultar_nivel_por_edad(20, settings=_supa_settings())
    assert r is not None
    assert r.nivel == "infants"
    assert r.edad_min_meses == 18
    assert r.edad_max_meses == 24
    assert r.rango_legible() == "18 meses a 2 años"


@pytest.mark.asyncio
@respx.mock
async def test_consultar_nivel_por_edad_no_match() -> None:
    """200 meses (16+ años) → ningún nivel."""
    respx.get("https://x.supabase.co/rest/v1/niveles_por_edad").mock(
        return_value=httpx.Response(200, json=[])
    )
    r = await consultar_nivel_por_edad(200, settings=_supa_settings())
    assert r is None


@pytest.mark.asyncio
async def test_consultar_nivel_por_edad_sin_supabase() -> None:
    """Sin supabase_url, devuelve None graciosamente."""
    from app.config import Settings

    r = await consultar_nivel_por_edad(20, settings=Settings())
    assert r is None


@pytest.mark.asyncio
@respx.mock
async def test_consultar_edades_de_nivel_directo() -> None:
    respx.get("https://x.supabase.co/rest/v1/niveles_por_edad").mock(
        return_value=httpx.Response(200, json=[_nivel_row()])
    )
    r = await consultar_edades_de_nivel("infants", settings=_supa_settings())
    assert r is not None
    assert r.nombre_display == "Infants"


@pytest.mark.asyncio
@respx.mock
async def test_consultar_edades_de_nivel_fallback_ilike() -> None:
    """Si match exacto falla, busca por nombre_display ilike."""
    # Primera llamada (match exacto) devuelve vacío; segunda devuelve resultado.
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[_nivel_row(nivel="infants", nombre_display="Infants")])

    respx.get("https://x.supabase.co/rest/v1/niveles_por_edad").mock(side_effect=handler)
    r = await consultar_edades_de_nivel("Infants Maternal", settings=_supa_settings())
    assert r is not None
    assert call_count["n"] == 2


@pytest.mark.asyncio
@respx.mock
async def test_listar_niveles_vigentes() -> None:
    respx.get("https://x.supabase.co/rest/v1/niveles_por_edad").mock(
        return_value=httpx.Response(
            200,
            json=[
                _nivel_row(
                    nivel="cubs_baby",
                    nombre_display="Cubs Baby",
                    edad_min_meses=3,
                    edad_max_meses=11,
                ),
                _nivel_row(
                    nivel="baby", nombre_display="Baby", edad_min_meses=12, edad_max_meses=18
                ),
                _nivel_row(
                    nivel="infants", nombre_display="Infants", edad_min_meses=18, edad_max_meses=24
                ),
            ],
        )
    )
    niveles = await listar_niveles_vigentes(settings=_supa_settings())
    assert len(niveles) == 3
    assert [n.nivel for n in niveles] == ["cubs_baby", "baby", "infants"]


# ============================================================
# becas
# ============================================================


@pytest.mark.asyncio
@respx.mock
async def test_get_becas() -> None:
    respx.get("https://x.supabase.co/rest/v1/becas").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "tipo": "hermanos_2do",
                    "porcentaje": "10.00",
                    "descripcion": "10% segundo hijo",
                    "vigente": True,
                },
                {
                    "tipo": "hermanos_3ro",
                    "porcentaje": "15.00",
                    "descripcion": "15% tercero",
                    "vigente": True,
                },
                {
                    "tipo": "socioeconomica",
                    "porcentaje": None,
                    "descripcion": "Proceso formal interno",
                    "vigente": True,
                },
            ],
        )
    )
    becas = await get_becas(settings=_supa_settings())
    assert len(becas) == 3
    socio = next(b for b in becas if b.tipo == "socioeconomica")
    assert socio.porcentaje is None


# ============================================================
# send_image / send_sticker
# ============================================================


@pytest.mark.asyncio
async def test_enviar_imagen_costos_kinder_llama_canal() -> None:
    channel = AsyncMock()
    channel.send_image = AsyncMock(return_value=None)
    ok = await enviar_imagen_costos_kinder(channel, "web:test", caption="costos kinder")
    assert ok is True
    channel.send_image.assert_awaited_once()
    args = channel.send_image.await_args
    assert args.kwargs["session_id"] == "web:test"
    assert "drive.google.com" in args.kwargs["image_url"]


@pytest.mark.asyncio
async def test_enviar_imagen_retorna_false_si_falla() -> None:
    channel = AsyncMock()
    channel.send_image = AsyncMock(side_effect=Exception("boom"))
    ok = await enviar_imagen_costos_kinder(channel, "web:test")
    assert ok is False


@pytest.mark.asyncio
async def test_enviar_sticker_despedida() -> None:
    channel = AsyncMock()
    channel.send_sticker = AsyncMock(return_value=None)
    ok = await enviar_sticker_despedida(channel, "telegram:99")
    assert ok is True
    channel.send_sticker.assert_awaited_once()


# ============================================================
# calendar
# ============================================================


@pytest.mark.asyncio
async def test_calendar_simulado_si_sin_oauth() -> None:
    """Sin OAuth creds, agendar devuelve EventoAgendado(simulado=True)."""
    from datetime import datetime

    tool = CalendarTool(settings=Settings())
    assert tool.is_configured() is False
    fecha = datetime(2026, 5, 25, 10, 0)
    evento = await tool.agendar_cita(
        nombre_papa="Juan",
        nombre_hijo="Mateo",
        nivel="primaria",
        fecha=fecha,
        campus="Campus 1",
    )
    assert evento.simulado is True
    assert evento.fecha == fecha
    assert evento.campus == "Campus 1"
    assert evento.evento_id.startswith("sim-")


def test_calendar_is_configured_requires_all_three() -> None:
    """Hace falta client_id + client_secret + refresh_token."""
    s = Settings(
        google_oauth_client_id="cid",
        google_oauth_client_secret="secret",
        google_oauth_refresh_token="refresh",
    )
    assert CalendarTool(settings=s).is_configured() is True

    s2 = Settings(google_oauth_client_id="cid", google_oauth_client_secret="secret")
    assert CalendarTool(settings=s2).is_configured() is False
