"""Configuration file, application paths and the SMTP password.

Everything lives on the user's machine: the config file (profile + email settings) in
the user config directory, the request database and generated files in the user data
directory (platformdirs). GOMME_HOME puts both under one directory instead, for tests
or a portable install. Files are created with mode 600.

The SMTP password is never written in the config file: it is read from an environment
variable (`password_env`), or from the system keyring when the optional `keyring`
package is installed.
"""

import contextlib
import importlib
import os
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Self

import platformdirs
import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    ValidationError,
    model_validator,
)

from gomme.brokers.registry import describe_yaml_error, load_strict_yaml
from gomme.brokers.schema import NonBlankStr
from gomme.privacy import is_private, write_private_file
from gomme.profile import Profile

APP_NAME = "gomme"
HOME_ENV = "GOMME_HOME"
CONFIG_FILE = "config.yml"
DEFAULT_PASSWORD_ENV = "GOMME_SMTP_PASSWORD"  # noqa: S105 - a variable name, not a secret
KEYRING_SERVICE = "gomme-smtp"

EnvVarName = Annotated[str, StringConstraints(pattern=r"^[A-Z_][A-Z0-9_]*$")]


class ConfigError(Exception):
    """The configuration is missing or invalid. The message never contains its values."""


def config_dir() -> Path:
    home = os.environ.get(HOME_ENV)
    return Path(home) / "config" if home else platformdirs.user_config_path(APP_NAME)


def data_dir() -> Path:
    home = os.environ.get(HOME_ENV)
    return Path(home) / "data" if home else platformdirs.user_data_path(APP_NAME)


def config_path() -> Path:
    return config_dir() / CONFIG_FILE


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SmtpSecurity(StrEnum):
    STARTTLS = "starttls"  # usually port 587
    SSL = "ssl"  # usually port 465


class EmailMode(StrEnum):
    SMTP = "smtp"  # Gomme sends through the user's own SMTP server
    MANUAL = "manual"  # Gomme writes .eml files the user sends from their mail client


class Composer(StrEnum):
    """Where the web interface opens a request for the user to send it (manual mode)."""

    GMAIL = "gmail"  # Gmail's compose window, in the browser
    MAIL_APP = "mail_app"  # the system mail application (mailto: link)


class SmtpConfig(_StrictModel):
    host: NonBlankStr
    port: Annotated[StrictInt, Field(gt=0, lt=65536)] = 587
    security: SmtpSecurity = SmtpSecurity.STARTTLS
    username: NonBlankStr
    password_env: EnvVarName = DEFAULT_PASSWORD_ENV

    @model_validator(mode="before")
    @classmethod
    def _no_password_in_the_file(cls, data: Any) -> Any:
        if isinstance(data, dict) and "password" in data:
            raise ValueError(
                "le mot de passe ne doit pas figurer dans le fichier : le mettre dans la "
                f"variable d'environnement indiquée par password_env ({DEFAULT_PASSWORD_ENV} "
                "par défaut) ou dans le trousseau du système"
            )
        return data


class EmailConfig(_StrictModel):
    mode: EmailMode = EmailMode.MANUAL
    composer: Composer = Composer.MAIL_APP
    smtp: SmtpConfig | None = None
    # Spread sending over several days: providers limit daily volume (Gmail: about 500,
    # less for new accounts) and flag bursts. Eraser uses 250 per day with Gmail.
    daily_limit: Annotated[StrictInt, Field(gt=0, le=500)] = 50
    delay_seconds: Annotated[float, Field(ge=0, le=600)] = 10.0
    bcc_self: bool = True  # a copy to the user's own address, as proof of sending

    @model_validator(mode="after")
    def _smtp_settings_for_smtp_mode(self) -> Self:
        if self.mode is EmailMode.SMTP and self.smtp is None:
            raise ValueError("mode smtp : la section smtp est obligatoire")
        return self


class RequestOptions(_StrictModel):
    access: bool = False  # also ask what data is held and where it came from (art. 15)
    notify_recipients: bool = True  # ask to pass the erasure on to recipients (art. 19)


class MonitorConfig(_StrictModel):
    # Brokers often list people again after a removal: check back after 60 to 90 days.
    recheck_after_days: Annotated[StrictInt, Field(ge=30, le=365)] = 75


class Config(_StrictModel):
    profile: Profile
    email: EmailConfig = EmailConfig()
    requests: RequestOptions = RequestOptions()
    monitor: MonitorConfig = MonitorConfig()


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"aucune configuration dans {path} : lancer « gomme init »") from None
    except OSError as exc:
        raise ConfigError(f"{path} illisible ({exc.strerror})") from None
    try:
        raw = load_strict_yaml(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} : {describe_yaml_error(exc)}") from None
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        problems = "\n".join(
            f"  • {'.'.join(map(str, error['loc'])) or 'racine'} : {error['msg']}"
            for error in exc.errors(include_url=False, include_input=False)
        )
        raise ConfigError(f"{path} invalide :\n{problems}") from None


def dump_config(config: Config) -> str:
    data = config.model_dump(mode="json", exclude_defaults=True)
    header = (
        "# Configuration de Gomme. Ce fichier contient vos données personnelles :\n"
        "# il reste sur votre machine, lisible par vous seul (mode 600).\n"
        "# Le mot de passe SMTP n'y figure jamais (voir password_env).\n"
    )
    return header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def save_config(config: Config, path: Path | None = None) -> Path:
    path = path or config_path()
    write_private_file(path, dump_config(config))
    return path


def config_is_private(path: Path | None = None) -> bool:
    return is_private(path or config_path())


class PasswordStoreError(Exception):
    """The system keyring is missing or refused the operation."""


class SystemKeyring:
    """The SMTP password in the system keyring (optional `keyring` package).

    Secret Service (GNOME, KDE), macOS Keychain or Windows Credential Locker: the password
    is encrypted by the system, never written by Gomme.
    """

    def _module(self) -> Any:
        try:
            return importlib.import_module("keyring")
        except ImportError:
            return None

    def available(self) -> bool:
        return self._module() is not None

    def get(self, username: str) -> str | None:
        keyring = self._module()
        if keyring is None:
            return None
        try:
            stored = keyring.get_password(KEYRING_SERVICE, username)
        except Exception:  # any backend failure (locked, no service): as if nothing stored
            return None
        return str(stored) if stored else None

    def set(self, username: str, password: str) -> None:
        keyring = self._module()
        if keyring is None:
            raise PasswordStoreError("le paquet keyring n'est pas installé")
        try:
            keyring.set_password(KEYRING_SERVICE, username, password)
        except Exception as exc:
            raise PasswordStoreError(f"trousseau indisponible ({type(exc).__name__})") from None

    def delete(self, username: str) -> None:
        keyring = self._module()
        if keyring is None:
            return
        with contextlib.suppress(Exception):
            keyring.delete_password(KEYRING_SERVICE, username)


def smtp_password(smtp: SmtpConfig, store: SystemKeyring | None = None) -> str:
    """The password from the environment, else from the keyring if available."""
    password = os.environ.get(smtp.password_env)
    if password:
        return password
    store = store or SystemKeyring()
    stored = store.get(smtp.username)
    if stored:
        return stored
    raise ConfigError(
        f"mot de passe SMTP introuvable : définir la variable {smtp.password_env}"
        + (f" ou l'enregistrer dans le trousseau ({KEYRING_SERVICE})" if store.available() else "")
    )
