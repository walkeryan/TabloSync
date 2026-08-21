from pathlib import Path

import pytest

from tablosync.config import ConfigurationError, Settings


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
