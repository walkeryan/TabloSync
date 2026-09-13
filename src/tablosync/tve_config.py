"""Configuration for the separate TV Everywhere virtual tuner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import ConfigurationError, _boolean, _integer, _optional


@dataclass(frozen=True, slots=True)
class TVESettings:
    friendly_name: str = "TabloSync TV Everywhere"
    port: int = 5005
    advertise_url: str | None = None
    state_file: Path = Path("/data/fox-auth.json")
    tuner_count: int = 1
    btn_guide_number: str = "6100"
    fox_call_sign: str | None = None
    fox_guide_number: str = "6101"
    fox_name: str = "Fox"
    ffmpeg_path: str = "ffmpeg"
    auth_ui: bool = True

    @classmethod
    def from_env(cls) -> TVESettings:
        advertise_url = _optional("TABLOSYNC_TVE_ADVERTISE_URL")
        if advertise_url and not advertise_url.startswith(("http://", "https://")):
            raise ConfigurationError("TABLOSYNC_TVE_ADVERTISE_URL must be an http(s) URL")

        guide_number = _optional("TABLOSYNC_TVE_BTN_GUIDE_NUMBER") or "6100"
        if not guide_number.replace(".", "", 1).isdigit():
            raise ConfigurationError("TABLOSYNC_TVE_BTN_GUIDE_NUMBER must be numeric")

        fox_guide_number = _optional("TABLOSYNC_TVE_FOX_GUIDE_NUMBER") or "6101"
        if not fox_guide_number.replace(".", "", 1).isdigit():
            raise ConfigurationError("TABLOSYNC_TVE_FOX_GUIDE_NUMBER must be numeric")

        return cls(
            friendly_name=(_optional("TABLOSYNC_TVE_FRIENDLY_NAME") or "TabloSync TV Everywhere"),
            port=_integer("TABLOSYNC_TVE_PORT", 5005, minimum=1, maximum=65535),
            advertise_url=advertise_url.rstrip("/") if advertise_url else None,
            state_file=Path(_optional("TABLOSYNC_TVE_STATE_FILE") or "/data/fox-auth.json"),
            tuner_count=_integer("TABLOSYNC_TVE_TUNER_COUNT", 1, minimum=1, maximum=4),
            btn_guide_number=guide_number,
            fox_call_sign=_optional("TABLOSYNC_TVE_FOX_CALL_SIGN"),
            fox_guide_number=fox_guide_number,
            fox_name=_optional("TABLOSYNC_TVE_FOX_NAME") or "Fox",
            ffmpeg_path=_optional("TABLOSYNC_TVE_FFMPEG_PATH") or "ffmpeg",
            auth_ui=_boolean("TABLOSYNC_TVE_AUTH_UI", True),
        )
