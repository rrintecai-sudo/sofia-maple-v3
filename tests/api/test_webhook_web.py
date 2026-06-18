"""Tests de los endpoints `/chat` y `/webhook/web` (web chat).

Foco en el fix de cookie de sesión (bug crítico post-Bloque 2): el
`HTMLResponse` retornado por `/chat` AHORA setea correctamente el header
`Set-Cookie: sofia_web_session=...`. Sin este fix, cada POST llegaba sin
cookie y el endpoint generaba un UUID nuevo por request, dejando cada
turno como t=0.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from app.api.webhook_web import router as web_router
from app.core.orchestrator import TurnResult
from app.core.state import Canal, FaseJourney
from fastapi import FastAPI
from fastapi.testclient import TestClient


@asynccontextmanager
async def _noop_lifespan(app: FastAPI):
    yield


@pytest.fixture
def client(tmp_path):
    """App de test sin lifespan real. Inyecta un chat.html mínimo en
    `web/templates/` para que el endpoint pueda servir.
    """
    # Asegura que web/templates/chat.html existe (en el repo real ya está,
    # pero por si los tests corren desde otro working dir).
    from pathlib import Path

    templates_dir = Path(__file__).resolve().parent.parent.parent / "web" / "templates"
    assert (templates_dir / "chat.html").exists(), (
        "El template chat.html debe existir en web/templates/"
    )

    app = FastAPI(lifespan=_noop_lifespan)
    app.include_router(web_router)
    with TestClient(app) as c:
        yield c


# ============================================================
# Fix bug crítico: cookie de sesión
# ============================================================


def test_chat_page_set_cookie_en_primera_visita(client: TestClient) -> None:
    """GET /chat sin cookie → response trae Set-Cookie con sofia_web_session.

    Bug regression: antes, set_cookie sobre el parámetro `response: Response`
    no se transfería al HTMLResponse retornado. El cliente nunca recibía la
    cookie.
    """
    resp = client.get("/chat")
    assert resp.status_code == 200
    # Header Set-Cookie debe estar presente
    set_cookie = resp.headers.get("set-cookie", "")
    assert "sofia_web_session=" in set_cookie, (
        f"Set-Cookie ausente o sin sofia_web_session. Header: {set_cookie!r}"
    )
    # Atributos críticos
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()
    assert "max-age=" in set_cookie.lower()


def test_chat_page_no_set_cookie_si_cliente_ya_tiene(client: TestClient) -> None:
    """Si el cliente envía sofia_web_session, NO se setea cookie nueva."""
    sid = "11111111-2222-3333-4444-555555555555"
    resp = client.get("/chat", cookies={"sofia_web_session": sid})
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "")
    # No debería re-setear la cookie si el cliente ya la tiene
    assert "sofia_web_session=" not in set_cookie, (
        f"NO debería re-setear cookie. Header: {set_cookie!r}"
    )
    # El SESSION_ID injectado en HTML debería ser el del cliente
    assert sid in resp.text


def test_chat_page_session_id_consistente_entre_get_y_html(
    client: TestClient,
) -> None:
    """El session_id en la cookie debe coincidir con el inyectado en HTML."""
    resp = client.get("/chat")
    set_cookie = resp.headers.get("set-cookie", "")
    # Extraer el UUID de la cookie
    m = re.search(r"sofia_web_session=([0-9a-f-]{36})", set_cookie)
    assert m, f"No se pudo extraer UUID de cookie: {set_cookie!r}"
    cookie_uuid = m.group(1)
    # El HTML debe inyectar `web:<uuid>` igual
    assert f"web:{cookie_uuid}" in resp.text


def test_post_webhook_usa_cookie_si_se_envia(client: TestClient) -> None:
    """POST con cookie envía mismo session_id al orchestrator."""
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    fake_result = TurnResult(
        response="ok",
        session_id=f"web:{sid}",
        fase_journey=FaseJourney.BIENVENIDA,
    )

    with patch(
        "app.api.webhook_web.procesar_turno",
        new_callable=AsyncMock,
        return_value=fake_result,
    ) as mock_proc:
        resp = client.post(
            "/webhook/web",
            json={"content": "hola"},
            cookies={"sofia_web_session": sid},
        )
    assert resp.status_code == 200
    # session_id que llega al orchestrator debe ser web:<cookie>
    mock_proc.assert_called_once()
    call_kwargs = mock_proc.call_args.kwargs
    assert call_kwargs["session_id"] == f"web:{sid}"
    assert call_kwargs["canal"] == Canal.WEB
    # Response también lo refleja
    assert resp.json()["session_id"] == f"web:{sid}"


def test_post_webhook_genera_uuid_fallback_sin_cookie(client: TestClient) -> None:
    """POST sin cookie → genera UUID nuevo (fallback). Comportamiento previo."""
    fake_result = TurnResult(
        response="ok",
        session_id="placeholder",
        fase_journey=FaseJourney.BIENVENIDA,
    )

    with patch(
        "app.api.webhook_web.procesar_turno",
        new_callable=AsyncMock,
        return_value=fake_result,
    ) as mock_proc:
        resp = client.post("/webhook/web", json={"content": "hola"})

    assert resp.status_code == 200
    mock_proc.assert_called_once()
    sid = mock_proc.call_args.kwargs["session_id"]
    # Debe ser web:<uuid v4>
    assert sid.startswith("web:")
    uuid_part = sid[len("web:") :]
    assert re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", uuid_part)


def test_posts_con_misma_cookie_usan_mismo_session_id(client: TestClient) -> None:
    """Dos POSTs consecutivos con la misma cookie → mismo session_id en ambos.

    Test crítico del fix: simula el flujo que fallaba (cada mensaje creaba
    un session_id nuevo).
    """
    sid = "ffffffff-0000-1111-2222-333333333333"
    fake_result = TurnResult(
        response="ok", session_id=f"web:{sid}", fase_journey=FaseJourney.BIENVENIDA
    )

    sids_capturados: list[str] = []

    async def _capture(**kwargs):
        sids_capturados.append(kwargs["session_id"])
        return fake_result

    with patch("app.api.webhook_web.procesar_turno", side_effect=_capture):
        client.post(
            "/webhook/web",
            json={"content": "hola"},
            cookies={"sofia_web_session": sid},
        )
        client.post(
            "/webhook/web",
            json={"content": "segundo mensaje"},
            cookies={"sofia_web_session": sid},
        )

    assert len(sids_capturados) == 2
    assert sids_capturados[0] == sids_capturados[1] == f"web:{sid}"


# ============================================================
# Fix B.3 (2026-05-22, feedback Lily): render correcto de markdown
# en mensajes del assistant. Antes: solo *texto* WhatsApp; Sofía generaba
# **texto** estándar que quedaba visible con asteriscos.
# ============================================================


def test_chat_js_soporta_markdown_bold_doble_asterisco() -> None:
    """El JS servido en /static/chat.js debe convertir **texto** a <strong>."""
    from pathlib import Path

    chat_js = (
        Path(__file__).resolve().parent.parent.parent / "web" / "static" / "chat.js"
    ).read_text(encoding="utf-8")
    # La regex de bold doble-asterisco debe estar presente
    assert "\\*\\*([^*\\n]+?)\\*\\*" in chat_js, (
        "El JS no contiene la regex de **bold** estándar Markdown"
    )
    # Y el reemplazo a <strong>
    assert "<strong>$1</strong>" in chat_js
    # La función formatBubble debe existir
    assert "function formatBubble" in chat_js


def test_chat_js_orden_bold_antes_italic() -> None:
    """Crítico: **bold** debe procesarse ANTES de *italic*. Si italic va
    primero, rompe la captura de bold."""
    from pathlib import Path

    chat_js = (
        Path(__file__).resolve().parent.parent.parent / "web" / "static" / "chat.js"
    ).read_text(encoding="utf-8")
    idx_bold = chat_js.find("\\*\\*([^*\\n]+?)\\*\\*")
    idx_italic_star = chat_js.find("(?<![*\\w])\\*(")
    assert idx_bold != -1, "Falta regex de bold"
    if idx_italic_star != -1:
        assert idx_bold < idx_italic_star, "**bold** debe ir antes que *italic* en formatBubble"


# ============================================================
# Saludo estático del HTML — anti-regresión del Fix A.2
# (commit ee58d2a cambió bienvenida.md; el HTML quedó desfasado hasta
# este fix)
# ============================================================


def _chat_html_text() -> str:
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent.parent / "web" / "templates" / "chat.html"
    return path.read_text(encoding="utf-8")


def test_chat_html_no_contiene_saludo_viejo() -> None:
    """Fix A.2 (ee58d2a, 21-may): el saludo viejo '¿qué te trajo a
    buscarnos?' fue eliminado del prompt. El HTML estático del web chat
    debe seguir la misma línea — Gaby/Cecilia (19-may) reportaron la
    frase como genérica y desconectada del descubrimiento.
    """
    html = _chat_html_text()
    assert "qué te trajo a buscarnos" not in html.lower(), (
        "regresión: el saludo viejo volvió al HTML estático"
    )


def test_chat_html_usa_saludo_nuevo_de_bienvenida_md() -> None:
    """El primer mensaje del bot en el HTML debe matchear EXACTAMENTE la
    apertura 'Primer contacto sin contexto' de
    app/core/prompts/journey/bienvenida.md."""
    from pathlib import Path

    bienvenida = (
        Path(__file__).resolve().parent.parent.parent
        / "app"
        / "core"
        / "prompts"
        / "journey"
        / "bienvenida.md"
    ).read_text(encoding="utf-8")

    # Frase canónica extraída de bienvenida.md (sin emojis, sin negritas)
    frase_canonica = (
        "¡Hola! Qué gusto que nos escribas. Soy Sofía, del equipo de "
        "admisiones de Maple Collège. Cuéntame, ¿para qué nivel te interesa "
        "información?"
    )
    # Sanity: la frase canónica vive en bienvenida.md
    assert frase_canonica in bienvenida, (
        "bienvenida.md no contiene la frase canónica esperada; "
        "revisar 'Primer contacto sin contexto'"
    )

    html = _chat_html_text()
    assert frase_canonica in html, (
        "El HTML estático debe contener la misma frase de bienvenida.md "
        "para que el primer load no muestre un saludo distinto al que "
        "Sofía generaría."
    )
