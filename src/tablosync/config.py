"""Environment-backed service configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    """Raised when required configuration is absent or invalid."""


def _optional(name: str) -> str | None:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else None


def _required(name: str) -> str:
    value = _optional(name)
    if value is None:
        raise ConfigurationError(f"{name} is required")
    return value


def _secret(name: str) -> str:
    direct = _optional(name)
    file_name = _optional(f"{name}_FILE")
    if direct and file_name:
        raise ConfigurationError(f"Set {name} or {name}_FILE, not both")
    if direct:
        return direct
    if file_name:
        try:
            value = Path(file_name).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigurationError(f"Could not read {name}_FILE") from exc
        if value:
            return value
    raise ConfigurationError(f"{name} or {name}_FILE is required")


def _boolean(name: str, default: bool) -> bool:
    value = _optional(name)
    if value is None:
        return default
    normalized = value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _integer(name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = _optional(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return parsed


@dataclass(frozen=True, slots=True)
class Settings:
    email: str
    password: str
    device_sid: str | None = None
    include_ott: bool = False
    friendly_name: str = "TabloSync"
    port: int = 5004
    channel_cache_seconds: int = 3600
    tuner_count_override: int | None = None
    advertise_url: str | None = None
    ffmpeg_path: str = "ffmpeg"

    @classmethod
    def from_env(cls) -> Settings:
        tuner_count = _optional("TABLOSYNC_TUNER_COUNT")
        advertise_url = _optional("TABLOSYNC_ADVERTISE_URL")
        if advertise_url and not advertise_url.startswith(("http://", "https://")):
            raise ConfigurationError("TABLOSYNC_ADVERTISE_URL must be an http(s) URL")

        return cls(
            email=_required("TABLO_EMAIL"),
            password=_secret("TABLO_PASSWORD"),
            device_sid=_optional("TABLO_DEVICE_SID"),
            include_ott=_boolean("TABLOSYNC_INCLUDE_OTT", False),
            friendly_name=_optional("TABLOSYNC_FRIENDLY_NAME") or "TabloSync",
            port=_integer("TABLOSYNC_PORT", 5004, minimum=1, maximum=65535),
            channel_cache_seconds=_integer(
                "TABLOSYNC_CHANNEL_CACHE_SECONDS", 3600, minimum=30, maximum=86400
            ),
            tuner_count_override=(
                _integer("TABLOSYNC_TUNER_COUNT", 2, minimum=1, maximum=8) if tuner_count else None
            ),
            advertise_url=advertise_url.rstrip("/") if advertise_url else None,
            ffmpeg_path=_optional("TABLOSYNC_FFMPEG_PATH") or "ffmpeg",
        )
