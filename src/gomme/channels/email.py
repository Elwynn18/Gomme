"""Email channel: build the message, write it as .eml (manual mode), or send it by SMTP.

Messages go straight from the user's machine to the user's own SMTP server: no third
party in between. In manual mode Gomme never sees the mail password at all.
"""

import contextlib
import smtplib
import ssl
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from gomme.compose import RequestText
from gomme.config import SmtpConfig, SmtpSecurity
from gomme.privacy import write_private_file

SMTP_TIMEOUT_SECONDS = 30


def build_message(
    text: RequestText,
    *,
    to: str,
    sender_name: str,
    sender_address: str,
    now: datetime,
    in_reply_to: str | None = None,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = formataddr((sender_name, sender_address))
    message["To"] = to
    message["Subject"] = text.subject
    message["Date"] = format_datetime(now)
    message["Message-ID"] = make_msgid(domain=sender_address.rpartition("@")[2])
    if in_reply_to:  # a reminder joins the thread of the original request
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to
    message.set_content(text.body, charset="utf-8")
    message["Content-Language"] = text.language  # after set_content(), which resets Content-*
    return message


def write_eml(message: EmailMessage, path: Path) -> Path:
    """Save the message (mode 600: it contains personal data), to open in a mail client."""
    write_private_file(path, message.as_string())
    return path


def mailto_link(text: RequestText, to: str) -> str:
    return f"mailto:{quote(to, safe='@')}?subject={quote(text.subject)}&body={quote(text.body)}"


class SmtpClient(Protocol):
    """The part of smtplib.SMTP used here, so tests can provide a fake server."""

    def ehlo(self) -> object: ...
    def starttls(self, *, context: ssl.SSLContext) -> object: ...
    def login(self, user: str, password: str) -> object: ...
    def send_message(
        self, msg: EmailMessage, from_addr: str | None = None, to_addrs: list[str] | None = None
    ) -> object: ...
    def quit(self) -> object: ...


SmtpFactory = Callable[[SmtpConfig, ssl.SSLContext], SmtpClient]


def connect(config: SmtpConfig, context: ssl.SSLContext) -> SmtpClient:
    """A real connection, with certificate verification."""
    if config.security is SmtpSecurity.SSL:
        return smtplib.SMTP_SSL(
            config.host, config.port, timeout=SMTP_TIMEOUT_SECONDS, context=context
        )
    return smtplib.SMTP(config.host, config.port, timeout=SMTP_TIMEOUT_SECONDS)


class SendError(Exception):
    """Sending failed. The message names the server, never the content."""


class RecipientRefusedError(SendError):
    """The server refused the broker's address: only this message failed, not the session."""


@dataclass
class SmtpSender:
    """One SMTP session for a batch of messages. Use as a context manager."""

    config: SmtpConfig
    password: str
    factory: SmtpFactory = connect
    delay_seconds: float = 0.0
    sleep: Callable[[float], None] = time.sleep
    _client: SmtpClient | None = None
    _sent: int = 0

    def __enter__(self) -> "SmtpSender":
        context = ssl.create_default_context()
        try:
            client = self.factory(self.config, context)
            if self.config.security is SmtpSecurity.STARTTLS:
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
            client.login(self.config.username, self.password)
        except (smtplib.SMTPException, OSError) as exc:
            raise SendError(
                f"connexion SMTP à {self.config.host}:{self.config.port} impossible "
                f"({type(exc).__name__})"
            ) from None
        self._client = client
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._client is not None:
            with contextlib.suppress(smtplib.SMTPException, OSError):
                self._client.quit()
            self._client = None

    def send(self, message: EmailMessage, *, bcc: Iterable[str] = ()) -> None:
        """Send to the To address, plus `bcc` (envelope only: not shown to the broker)."""
        if self._client is None:
            raise SendError("session SMTP fermée")
        if self._sent and self.delay_seconds:
            self.sleep(self.delay_seconds)  # spread messages: providers flag bursts
        to = str(message["To"])
        recipients = [to, *bcc]
        try:
            # Returns the recipients refused when at least one was accepted (smtplib).
            refused = self._client.send_message(message, to_addrs=recipients)
        except smtplib.SMTPRecipientsRefused as exc:
            refused = exc.recipients
        except (smtplib.SMTPException, OSError) as exc:
            raise SendError(f"envoi refusé par {self.config.host} ({type(exc).__name__})") from None
        self._sent += 1
        if isinstance(refused, dict) and to in refused:
            code = refused[to][0] if isinstance(refused[to], tuple) else "?"
            raise RecipientRefusedError(
                f"adresse du broker refusée par {self.config.host} (code {code})"
            )
