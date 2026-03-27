"""
config_loader.py – Robuster Konfigurations-Lader für das KI-Dokumenten-Archiv.

Lädt config.yaml (per Volume gemountet), überschreibt sensible Felder aus
Umgebungsvariablen und validiert alle Pflichtfelder beim Start.

Keine Pfade oder Hardware-IDs sind in diesem Modul hardcodiert.
Alle Standardwerte sind dokumentiert und überschreibbar.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pfad zur Konfigurationsdatei – über Umgebungsvariable überschreibbar
# ---------------------------------------------------------------------------
_DEFAULT_CONFIG_PATH = "/app/config.yaml"
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", _DEFAULT_CONFIG_PATH))


# ---------------------------------------------------------------------------
# Pydantic-Modelle für typsichere Konfiguration
# ---------------------------------------------------------------------------

class PathsConfig(BaseModel):
    scan_input_dir: str
    split_output_dir: str
    analyzed_output_dir: str
    quarantine_dir: str
    db_data_dir: str
    paperless_consume_dir: str = "/data/paperless/consume"
    tmp_dir: str = "/data/tmp"

    @field_validator(
        "scan_input_dir",
        "split_output_dir",
        "analyzed_output_dir",
        "quarantine_dir",
        "tmp_dir",
        mode="before",
    )
    @classmethod
    def must_be_absolute(cls, v: str) -> str:
        if not Path(v).is_absolute():
            raise ValueError(f"Pfad muss absolut sein: {v}")
        return v


class GpuConfig(BaseModel):
    vision_gpu_id: int = 0
    embedding_gpu_id: int = 1
    allow_cpu_fallback: bool = False

    @field_validator("vision_gpu_id", "embedding_gpu_id", mode="before")
    @classmethod
    def gpu_id_non_negative(cls, v: int) -> int:
        if int(v) < 0:
            raise ValueError("GPU-ID muss >= 0 sein.")
        return int(v)


class AiConfig(BaseModel):
    confidence_threshold: float = Field(0.75, ge=0.0, le=1.0)
    ocr_confidence_threshold: float = Field(0.80, ge=0.0, le=1.0)
    ocr_language: str = "deu"
    llm_model_id: str
    embedding_model_id: str
    llm_max_tokens: int = Field(512, ge=64, le=8192)
    embedding_batch_size: int = Field(32, ge=1, le=512)


class PersonsConfig(BaseModel):
    me: list[str] = Field(default_factory=list)
    partner: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def at_least_one_name(self) -> "PersonsConfig":
        if not self.me and not self.partner:
            raise ValueError(
                "persons.me oder persons.partner muss mindestens einen Namen enthalten."
            )
        return self


class DatabaseConfig(BaseModel):
    host: str = "mariadb"
    port: int = Field(3306, ge=1, le=65534)
    name: str = "document_archive"
    user: str = "archive_user"
    password: str = ""
    pool_size: int = Field(5, ge=1, le=50)
    hnsw_m: int = Field(16, ge=4, le=64)
    hnsw_ef_search: int = Field(64, ge=10, le=500)

    @model_validator(mode="after")
    def password_from_env(self) -> "DatabaseConfig":
        """Überschreibt das Passwort mit der Umgebungsvariable DB_PASSWORD,
        falls diese gesetzt ist."""
        env_pw = os.environ.get("DB_PASSWORD", "")
        if env_pw:
            self.password = env_pw
        if not self.password:
            raise ValueError(
                "Datenbankpasswort fehlt. Setze DB_PASSWORD als Umgebungsvariable."
            )
        return self


class PaperlessConfig(BaseModel):
    base_url: str = "http://paperless-ngx:8000"
    api_token: str = ""
    default_document_type: str = "Eingehend"

    @model_validator(mode="after")
    def token_from_env(self) -> "PaperlessConfig":
        """Überschreibt api_token mit PAPERLESS_API_TOKEN aus der Umgebung."""
        env_token = os.environ.get("PAPERLESS_API_TOKEN", "")
        if env_token:
            self.api_token = env_token
        return self


class WatcherConfig(BaseModel):
    expected_dpi: int = 600
    poll_interval_seconds: int = Field(5, ge=1, le=300)
    min_file_size_bytes: int = Field(10240, ge=1024)
    file_stable_wait_seconds: int = Field(3, ge=1, le=60)


class RagChatConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(8080, ge=1024, le=65534)
    top_k_results: int = Field(5, ge=1, le=100)
    cors_origins: str = "*"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "json"
    log_file: str = "/data/logs/archive.log"

    @field_validator("level")
    @classmethod
    def valid_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in allowed:
            raise ValueError(f"Log-Level muss eines von {allowed} sein, nicht: {v}")
        return v.upper()


class AppConfig(BaseModel):
    paths: PathsConfig
    gpu: GpuConfig
    ai: AiConfig
    persons: PersonsConfig
    tag_mapping: dict[str, str] = Field(default_factory=dict)
    database: DatabaseConfig
    paperless: PaperlessConfig
    watcher: WatcherConfig = Field(default_factory=WatcherConfig)
    rag_chat: RagChatConfig = Field(default_factory=RagChatConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


# ---------------------------------------------------------------------------
# Öffentliche Lade-Funktion
# ---------------------------------------------------------------------------

def load_config(config_path: Path | None = None) -> AppConfig:
    """Lädt und validiert die Konfiguration aus config.yaml.

    Args:
        config_path: Optionaler Pfad zur config.yaml.
                     Standard: CONFIG_PATH (aus Env-Var CONFIG_PATH oder
                     /app/config.yaml).

    Returns:
        Validiertes AppConfig-Objekt.

    Raises:
        FileNotFoundError: Wenn die Konfigurationsdatei nicht gefunden wird.
        ValueError: Bei ungültigen Konfigurationswerten.
    """
    path = Path(config_path) if config_path else CONFIG_PATH

    if not path.exists():
        raise FileNotFoundError(
            f"Konfigurationsdatei nicht gefunden: {path}\n"
            "Stelle sicher, dass config.yaml als Volume gemountet ist:\n"
            "  -v /host/path/config.yaml:/app/config.yaml:ro"
        )

    logger.info("Lade Konfiguration aus: %s", path)

    with path.open("r", encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}

    # Umgebungsvariablen überschreiben bestimmte Felder
    raw = _apply_env_overrides(raw)

    config = AppConfig(**raw)

    logger.info(
        "Konfiguration erfolgreich geladen. "
        "GPU Vision=%d, GPU Embedding=%d, Confidence=%.2f",
        config.gpu.vision_gpu_id,
        config.gpu.embedding_gpu_id,
        config.ai.confidence_threshold,
    )
    return config


def _apply_env_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """Überschreibt ausgewählte Konfigurationswerte aus Umgebungsvariablen.

    Dies ermöglicht es, die Docker-Compose-Umgebungsvariablen
    (VISION_GPU_ID, EMBEDDING_GPU_ID, etc.) direkt zu verwenden.
    """
    env_map = {
        "VISION_GPU_ID": ("gpu", "vision_gpu_id"),
        "EMBEDDING_GPU_ID": ("gpu", "embedding_gpu_id"),
        "SCAN_INPUT_DIR": ("paths", "scan_input_dir"),
        "SPLIT_OUTPUT_DIR": ("paths", "split_output_dir"),
        "ANALYZED_OUTPUT_DIR": ("paths", "analyzed_output_dir"),
        "QUARANTINE_DIR": ("paths", "quarantine_dir"),
        "DB_HOST": ("database", "host"),
        "DB_PORT": ("database", "port"),
        "DB_NAME": ("database", "name"),
        "DB_USER": ("database", "user"),
        "PAPERLESS_BASE_URL": ("paperless", "base_url"),
        "RAG_CHAT_PORT": ("rag_chat", "port"),
        "LOG_LEVEL": ("logging", "level"),
    }

    for env_key, (section, field) in env_map.items():
        env_val = os.environ.get(env_key)
        if env_val is not None:
            if section not in raw:
                raw[section] = {}
            raw[section][field] = env_val
            logger.debug(
                "Konfiguration '%s.%s' aus Umgebungsvariable %s gesetzt.",
                section,
                field,
                env_key,
            )

    return raw


def configure_logging(config: AppConfig) -> None:
    """Richtet das Python-Logging gemäß der Konfiguration ein."""
    log_level = getattr(logging, config.logging.level, logging.INFO)

    handlers: list[logging.Handler] = [logging.StreamHandler()]

    log_file = config.logging.log_file
    if log_file:
        log_dir = Path(log_file).parent
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    if config.logging.format == "json":
        fmt = (
            '{"time": "%(asctime)s", "level": "%(levelname)s", '
            '"service": "%(name)s", "message": "%(message)s"}'
        )
    else:
        fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    logging.basicConfig(level=log_level, format=fmt, handlers=handlers, force=True)
    logger.info("Logging initialisiert: level=%s, format=%s", config.logging.level, config.logging.format)
