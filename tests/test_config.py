from pathlib import Path

import pytest

from tablosync.config import ConfigurationError, Settings
from tablosync.tve_config import TVESettings


def test_loads_password_from_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = tmp_path / "tablo-password"
    secret.write_text("correct horse battery staple\n", encoding="utf-8")
    monkeypatch.setenv("TABLO_EMAIL", "viewer@example.com")
    monkeypatch.setenv("TABLO_PASSWORD_FILE", str(secret))

    settings = Settings.from_env()

    assert settings.password == "correct horse battery staple"


def test_rejects_direct_and_file_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABLO_EMAIL", "viewer@example.com")
    monkeypatch.setenv("TABLO_PASSWORD", "one")
    monkeypatch.setenv("TABLO_PASSWORD_FILE", "/tmp/two")

    with pytest.raises(ConfigurationError, match="not both"):
        Settings.from_env()


def test_rejects_invalid_advertise_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABLO_EMAIL", "viewer@example.com")
    monkeypatch.setenv("TABLO_PASSWORD", "secret")
    monkeypatch.setenv("TABLOSYNC_ADVERTISE_URL", "192.168.1.20:5004")

    with pytest.raises(ConfigurationError, match="http"):
        Settings.from_env()


def test_tve_settings_do_not_require_account_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TABLO_EMAIL", raising=False)
    monkeypatch.delenv("TABLO_PASSWORD", raising=False)
    settings = TVESettings.from_env()
    assert settings.port == 5005
    assert settings.btn_guide_number == "6100"


def test_tve_guide_number_must_be_numeric(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABLOSYNC_TVE_BTN_GUIDE_NUMBER", "BTN")
    with pytest.raises(ConfigurationError, match="GUIDE_NUMBER"):
        TVESettings.from_env()


def test_tve_optional_local_fox_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABLOSYNC_TVE_FOX_CALL_SIGN", "WYFX-LD")
    monkeypatch.setenv("TABLOSYNC_TVE_FOX_GUIDE_NUMBER", "6101")
    monkeypatch.setenv("TABLOSYNC_TVE_FOX_NAME", "Fox Youngstown")

    settings = TVESettings.from_env()

    assert settings.fox_call_sign == "WYFX-LD"
    assert settings.fox_guide_number == "6101"
    assert settings.fox_name == "Fox Youngstown"
