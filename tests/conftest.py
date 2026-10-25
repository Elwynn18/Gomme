"""Shared test fixtures and safety guards."""

import socket
import sys
from pathlib import Path
from typing import NoReturn

import pytest
from fakes import FakeSmtp

from gomme.profile import PostalAddress, Profile

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any network access fail loudly: tests must never reach a real host."""

    def guard(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("Network access is forbidden in tests")

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket.socket, "connect_ex", guard)
    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket, "getaddrinfo", guard)


@pytest.fixture(autouse=True)
def _no_system_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never read or write the developer's real keyring."""
    monkeypatch.setitem(sys.modules, "keyring", None)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Ignore the developer's own settings and files so tests stay reproducible and safe."""
    monkeypatch.delenv("GOMME_BROKERS_DIR", raising=False)
    monkeypatch.delenv("GOMME_SMTP_PASSWORD", raising=False)
    monkeypatch.setenv("GOMME_HOME", str(tmp_path / "gomme-home"))


@pytest.fixture
def fixtures_brokers_dir() -> Path:
    """Directory holding the fictional broker definitions."""
    return FIXTURES_DIR / "brokers"


@pytest.fixture
def profile() -> Profile:
    """A fictional user (example.org is a reserved domain)."""
    return Profile(
        first_name="Jeanne",
        last_name="Exemple",
        email="jeanne.exemple@example.org",
        country="FR",
        postal_address=PostalAddress(
            street="1 rue de l'Exemple", postal_code="00000", city="Nulle-Part", country="FR"
        ),
    )


@pytest.fixture
def fake_smtp() -> FakeSmtp:
    """A fake SMTP server: tests never reach a real one."""
    return FakeSmtp()
