"""Tests del helper parse_session_id."""

from __future__ import annotations

import pytest
from app.adapters.channel import parse_session_id


def test_parse_whatsapp() -> None:
    canal, ident = parse_session_id("whatsapp:5218441302112@s.whatsapp.net")
    assert canal == "whatsapp"
    assert ident == "5218441302112@s.whatsapp.net"


def test_parse_telegram() -> None:
    canal, ident = parse_session_id("telegram:123456")
    assert canal == "telegram"
    assert ident == "123456"


def test_parse_web() -> None:
    canal, ident = parse_session_id("web:abc-uuid-1234")
    assert canal == "web"
    assert ident == "abc-uuid-1234"


def test_parse_canal_invalido() -> None:
    with pytest.raises(ValueError, match="canal desconocido"):
        parse_session_id("signal:12345")


def test_parse_sin_prefijo() -> None:
    with pytest.raises(ValueError, match="sin prefijo"):
        parse_session_id("12345")
