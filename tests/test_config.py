"""Tests for the configuration file and the SMTP password."""

import stat
import sys
from pathlib import Path

import pytest
import yaml

from gomme.config import (
    Config,
    ConfigError,
    EmailConfig,
    EmailMode,
    SmtpConfig,
    config_is_private,
    config_path,
    data_dir,
    load_config,
    save_config,
    smtp_password,
)
from gomme.profile import Profile


def test_paths_follow_gomme_home(tmp_path: Path) -> None:
    assert config_path() == tmp_path / "gomme-home" / "config" / "config.yml"
    assert data_dir() == tmp_path / "gomme-home" / "data"


def test_round_trip_and_private_mode(profile: Profile) -> None:
    config = Config(profile=profile)

    path = save_config(config)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert config_is_private()
    assert load_config() == config


def test_missing_config_points_to_init() -> None:
    with pytest.raises(ConfigError, match="gomme init"):
        load_config()


def test_errors_do_not_echo_values(profile: Profile) -> None:
    path = save_config(Config(profile=profile))
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["profile"]["email"] = "adresse-secrete"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        load_config()

    assert "profile.email" in str(exc_info.value)
    assert "adresse-secrete" not in str(exc_info.value)


def test_password_is_refused_in_the_file(profile: Profile) -> None:
    path = save_config(Config(profile=profile))
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["email"] = {
        "mode": "smtp",
        "smtp": {"host": "smtp.example.org", "username": "u", "password": "secret"},
    }
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(ConfigError, match="mot de passe ne doit pas figurer") as exc_info:
        load_config()

    assert "secret" not in str(exc_info.value)


def test_smtp_mode_needs_smtp_settings() -> None:
    with pytest.raises(ValueError, match="section smtp"):
        EmailConfig(mode=EmailMode.SMTP)


def test_password_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    smtp = SmtpConfig(host="smtp.example.org", username="u", password_env="TEST_SMTP_PW")
    monkeypatch.setenv("TEST_SMTP_PW", "fictional-password")

    assert smtp_password(smtp) == "fictional-password"


def test_missing_password_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "keyring", None)  # no keyring
    smtp = SmtpConfig(host="smtp.example.org", username="u")

    with pytest.raises(ConfigError, match="GOMME_SMTP_PASSWORD"):
        smtp_password(smtp)
