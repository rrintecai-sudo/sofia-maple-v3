"""Configuración tipada cargada desde .env vía pydantic-settings."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración global de la aplicación.

    Lee de variables de entorno o de `.env` en la raíz del proyecto.
    Validada al arranque — si falta algo requerido, el contenedor no levanta.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Entorno ---
    env: Literal["development", "production", "test"] = "development"
    log_level: str = "INFO"
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    # --- LLM principales ---
    anthropic_api_key: str = ""
    anthropic_model_principal: str = "claude-haiku-4-5"
    anthropic_model_juez: str = "claude-sonnet-4-6"
    openai_api_key: str = ""
    openai_model_auxiliar: str = "gpt-4o-mini"
    openai_model_embeddings: str = "text-embedding-3-small"
    openai_embedding_dim: int = 1536

    # --- Supabase ---
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_key: str = ""
    supabase_db_url: str = ""
    supabase_pat: str = ""  # Personal Access Token para Management API (DDL)
    supabase_project_ref: str = ""

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"
    redis_debounce_window_seconds: int = 7

    # --- Telegram ---
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""

    # --- Evolution API (WhatsApp) ---
    evolution_base_url: str = ""
    evolution_instance: str = ""
    evolution_api_key: str = ""

    # --- Web Chat ---
    web_session_cookie: str = "sofia_web_session"
    web_chat_title: str = "Sofía — Maple Collège"

    # --- Admin / observabilidad ---
    admin_api_key: str = ""
    alert_daily_cost_usd: float = 5.0
    alert_p95_latency_ms: int = 15000

    # --- Notificaciones (Bloque C.1) ---
    # Email de Lily para recibir aviso de cita pendiente. Si está vacío,
    # el stub solo loggea (Maple Platform la verá igual en su dashboard
    # vía activity_events).
    lily_email: str = ""
    appointment_approval_url: str = ""  # link a la cita en Maple Platform
    # Resend — correo real de confirmación al papá (Mensaje 2 de Gaby). Si
    # `resend_api_key` está vacío, send_email cae al stub que solo loggea (el
    # correo NUNCA es load-bearing: la cita y el cierre D.4 se hacen igual).
    resend_api_key: str = ""
    email_from: str = "Maple Collège <notificaciones@maplecollege.rrintecai.co>"

    # --- Google Calendar ---
    google_calendar_id: str = "admisiones@maplesaltillo.com"
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_refresh_token: str = ""

    # --- Feature flags ---
    enable_prompt_caching: bool = True
    enable_validators: bool = True
    max_regenerations_per_turn: int = Field(default=2, ge=0, le=5)
    # Tope de preguntas en el texto libre de Haiku (guard de salida). Subir a 2 si
    # se decide permitir dos preguntas por turno.
    max_preguntas_por_turno: int = Field(default=1, ge=1, le=3)
    # Flujo de venta (3 etapas): nº de bloques de valor con continuación del papá antes
    # de que el código ordene a Haiku empujar la cita. Default 2 (~3 turnos), tope 3.
    umbral_empuje: int = Field(default=2, ge=2, le=3)

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def is_test(self) -> bool:
        return self.env == "test"


@lru_cache
def get_settings() -> Settings:
    """Devuelve la instancia singleton de Settings (cacheada)."""
    return Settings()
