"""Tablo account, device, channel, and stream access."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from tablo_api import TabloAuth, TabloClient
from tablo_api.models import TabloChannel, TabloDevice, TabloStream

from .config import Settings

logger = logging.getLogger(__name__)


class BridgeError(RuntimeError):
    """Raised when the selected Tablo cannot satisfy a request."""


class UnknownChannel(BridgeError):
    """Raised when a requested channel is not in the current Tablo lineup."""


@dataclass(frozen=True, slots=True)
class DeviceSummary:
    name: str
    sid: str
    local_url: str
    tuner_count: int


class TabloBridge:
    """Owns short-lived Tablo cloud credentials and a cached channel lineup."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._device: TabloDevice | None = None
        self._channels: list[TabloChannel] = []
        self._channels_at = 0.0
        self._tuner_count = settings.tuner_count_override or 2
        self._auth_lock = asyncio.Lock()
        self._channels_lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return self._device is not None

    @property
    def tuner_count(self) -> int:
        return self._tuner_count

    @property
    def summary(self) -> DeviceSummary:
        if self._device is None:
            raise BridgeError("Tablo is not initialized")
        return DeviceSummary(
            name=self._device.name,
            sid=self._device.sid,
            local_url=self._device.local_url,
            tuner_count=self._tuner_count,
        )

    async def initialize(self) -> None:
        try:
            await self._authenticate()
            await self.channels(force=True)
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError("Could not authenticate with the Tablo account or device") from exc

    async def _authenticate(self) -> None:
        async with self._auth_lock:
            devices = await asyncio.to_thread(
                TabloAuth(self.settings.email, self.settings.password).discover
            )
            if not devices:
                raise BridgeError("The Tablo account has no devices")

            device = self._select_device(devices)
            self._device = device
            self._channels_at = 0.0

            if self.settings.tuner_count_override is None:
                self._tuner_count = await self._read_tuner_count(device)

            logger.info(
                "Connected to Tablo %s at %s with %d tuner(s)",
                device.name,
                device.local_url,
                self._tuner_count,
            )

    def _select_device(self, devices: list[TabloDevice]) -> TabloDevice:
        if self.settings.device_sid:
            selected = next(
                (device for device in devices if device.sid == self.settings.device_sid), None
            )
            if selected is None:
                raise BridgeError("TABLO_DEVICE_SID does not match a device on this account")
            return selected

        if len(devices) > 1:
            choices = ", ".join(f"{device.name} ({device.sid})" for device in devices)
            raise BridgeError(
                "This account has multiple Tablo devices; set TABLO_DEVICE_SID to one of: "
                f"{choices}"
            )
        return devices[0]

    async def _read_tuner_count(self, device: TabloDevice) -> int:
        try:
            info: dict[str, Any] = await asyncio.to_thread(TabloClient(device).server_info)
            count = int(info.get("model", {}).get("tuners", 2))
            return count if 1 <= count <= 8 else 2
        except Exception as exc:  # Device firmware responses vary; stay conservative.
            logger.warning("Could not read Tablo tuner count; using 2: %s", exc)
            return 2

    async def channels(self, *, force: bool = False) -> list[TabloChannel]:
        if self._device is None:
            raise BridgeError("Tablo is not initialized")

        is_fresh = time.monotonic() - self._channels_at < self.settings.channel_cache_seconds
        if self._channels and is_fresh and not force:
            return list(self._channels)

        async with self._channels_lock:
            is_fresh = time.monotonic() - self._channels_at < self.settings.channel_cache_seconds
            if self._channels and is_fresh and not force:
                return list(self._channels)

            for attempt in range(2):
                try:
                    assert self._device is not None
                    client = TabloClient(self._device)
                    channels = await asyncio.to_thread(
                        client.channels, include_ott=self.settings.include_ott
                    )
                    self._channels = channels
                    self._channels_at = time.monotonic()
                    logger.info("Loaded %d Tablo channel(s)", len(channels))
                    return list(channels)
                except Exception as exc:
                    if attempt == 0:
                        logger.info("Refreshing Tablo authentication after lineup request failed")
                        await self._authenticate()
                        continue
                    raise BridgeError("Could not refresh the Tablo channel lineup") from exc
        raise BridgeError("Could not load the Tablo channel lineup")

    async def watch(self, identifier: str) -> TabloStream:
        channels = await self.channels()
        if not any(channel.identifier == identifier for channel in channels):
            raise UnknownChannel("Unknown channel")

        for attempt in range(2):
            try:
                if self._device is None:
                    raise BridgeError("Tablo is not initialized")
                return await asyncio.to_thread(TabloClient(self._device).watch, identifier)
            except BridgeError:
                raise
            except Exception as exc:
                if attempt == 0:
                    logger.info("Refreshing Tablo authentication after stream request failed")
                    await self._authenticate()
                    continue
                raise BridgeError("Could not start the Tablo stream") from exc
        raise BridgeError("Could not start the Tablo stream")
