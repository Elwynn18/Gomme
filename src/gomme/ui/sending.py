"""Automatic sending from the web interface: one background job at a time.

The emails go through the user's own SMTP server (Gmail with an app password, for
instance), one by one with a pause in between, within the daily limit. The password
comes from the environment, the system keyring, or the form; it is never written to
disk by Gomme, and is kept in memory only while the job runs.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


class PasswordStore(Protocol):
    """Where the SMTP password may be kept between sessions (the system keyring)."""

    def available(self) -> bool: ...
    def get(self, username: str) -> str | None: ...
    def set(self, username: str, password: str) -> None: ...
    def delete(self, username: str) -> None: ...


def in_background(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="gomme-send", daemon=True).start()


@dataclass
class SendJob:
    request_ids: tuple[int, ...]  # the queued emails when the job started
    planned: int  # how many it may send today (daily limit)
    delay_seconds: float
    started_at: datetime
    finished: bool = False
    sent: int = 0
    errors: list[str] = field(default_factory=list)  # one broker refused, for instance
    failure: str | None = None  # the whole session failed (password, connection)
    limit_reached: bool = False
    keyring_note: str = ""


class Jobs:
    """The current sending job, if any. Only one runs at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current: SendJob | None = None

    def running(self) -> bool:
        return self.current is not None and not self.current.finished

    def start(
        self,
        job: SendJob,
        work: Callable[[SendJob], None],
        run: Callable[[Callable[[], None]], None],
    ) -> bool:
        """Start `job` unless another one is running. Returns whether it started."""
        with self._lock:
            if self.running():
                return False
            self.current = job

        def guarded() -> None:
            try:
                work(job)
            except Exception as exc:  # the page must not wait forever
                job.failure = f"Erreur inattendue ({type(exc).__name__})."
            finally:
                job.finished = True

        run(guarded)
        return True


def explain_failure(message: str) -> str:
    """A session error from the SMTP channel, in plain words."""
    if "Authentication" in message:
        return (
            "Le serveur a refusé l'identifiant ou le mot de passe. Avec Gmail, il faut un "
            "mot de passe d'application (16 lettres), pas votre mot de passe habituel."
        )
    if "connexion SMTP" in message:
        return (
            "Impossible de joindre le serveur d'envoi. Vérifiez votre connexion à Internet "
            f"et les réglages du serveur. Détail : {message}"
        )
    return message
