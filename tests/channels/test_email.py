"""Tests for the email channel. No real server: a fake SMTP client records the calls."""

import email
import stat
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fakes import FakeSmtp

from gomme.channels.email import (
    SendError,
    SmtpSender,
    build_message,
    gmail_compose_link,
    mailto_link,
    write_eml,
)
from gomme.compose import RequestText
from gomme.config import SmtpConfig, SmtpSecurity
from gomme.legal.statutes import Jurisdiction

NOW = datetime(2026, 10, 25, 9, 30, tzinfo=UTC)
TEXT = RequestText(
    subject="Demande d'effacement et d'opposition (articles 17 et 21 du RGPD)",
    body="Madame, Monsieur,\n\nÉffacez mes données.\n\nJeanne Exemple\n",
    language="fr",
    jurisdiction=Jurisdiction.GDPR,
)
SMTP = SmtpConfig(host="smtp.example.org", username="jeanne")


def message() -> EmailMessage:
    return build_message(
        TEXT,
        to="dpo@example.org",
        sender_name="Jeanne Exemple",
        sender_address="jeanne.exemple@example.org",
        now=NOW,
    )


def test_message_headers_and_body() -> None:
    msg = message()

    assert msg["To"] == "dpo@example.org"
    assert msg["From"] == "Jeanne Exemple <jeanne.exemple@example.org>"
    assert msg["Subject"] == TEXT.subject
    assert msg["Message-ID"].endswith("@example.org>")
    assert msg["Content-Language"] == "fr"
    assert msg.get_content() == TEXT.body


def test_eml_is_private_and_readable_by_mail_clients(tmp_path: Path) -> None:
    path = write_eml(message(), tmp_path / "outbox" / "1-courtier.eml")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    parsed = email.message_from_string(path.read_text(encoding="utf-8"))
    assert parsed["To"] == "dpo@example.org"


def test_mailto_link_carries_subject_and_body() -> None:
    link = mailto_link(TEXT, "dpo@example.org")

    assert link.startswith("mailto:dpo@example.org?subject=")
    assert unquote(link).endswith(TEXT.body)


def test_gmail_link_opens_a_filled_compose_window() -> None:
    link = gmail_compose_link(TEXT, "dpo@example.org", account="jeanne.exemple@example.org")

    parts = urlsplit(link)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://mail.google.com/mail/"
    query = parse_qs(parts.query)
    assert query["authuser"] == ["jeanne.exemple@example.org"]
    assert query["view"] == ["cm"]
    assert query["to"] == ["dpo@example.org"]
    assert query["su"] == [TEXT.subject]
    assert query["body"] == [TEXT.body]
    assert "+" not in link  # spaces are %20: Gmail would show a "+" literally


def test_starttls_session_sends_with_bcc_to_the_user(fake_smtp: FakeSmtp) -> None:
    fake = fake_smtp
    sender = SmtpSender(SMTP, "fictional-password", factory=lambda _config, _context: fake)

    with sender:
        sender.send(message(), bcc=["jeanne.exemple@example.org"])

    assert fake.calls == ["ehlo", "starttls", "ehlo", "login:jeanne", "send", "quit"]
    (sent, recipients) = fake.sent[0]
    assert recipients == ["dpo@example.org", "jeanne.exemple@example.org"]
    assert "Bcc" not in sent  # the broker never sees the copy


def test_ssl_session_skips_starttls(fake_smtp: FakeSmtp) -> None:
    fake = fake_smtp
    config = SMTP.model_copy(update={"security": SmtpSecurity.SSL, "port": 465})

    with SmtpSender(config, "pw", factory=lambda _config, _context: fake) as sender:
        sender.send(message())

    assert "starttls" not in fake.calls


def test_messages_are_spaced_out(fake_smtp: FakeSmtp) -> None:
    fake, pauses = fake_smtp, []
    sender = SmtpSender(
        SMTP, "pw", factory=lambda _c, _x: fake, delay_seconds=10, sleep=pauses.append
    )

    with sender:
        for _ in range(3):
            sender.send(message())

    assert pauses == [10, 10]  # none before the first message


@pytest.mark.parametrize("step", ["starttls", "login:jeanne"])
def test_connection_errors_name_the_server_only(step: str, fake_smtp: FakeSmtp) -> None:
    fake = fake_smtp
    fake.fail_on = step

    with pytest.raises(SendError, match=r"smtp\.example\.org:587") as exc_info:
        SmtpSender(SMTP, "fictional-password", factory=lambda _c, _x: fake).__enter__()

    assert "fictional-password" not in str(exc_info.value)


def test_send_errors_are_reported(fake_smtp: FakeSmtp) -> None:
    fake = fake_smtp
    fake.fail_on = "send"

    with (
        pytest.raises(SendError, match="envoi refusé"),
        SmtpSender(SMTP, "pw", factory=lambda _c, _x: fake) as sender,
    ):
        sender.send(message())

    assert fake.calls[-1] == "quit"


def test_sending_outside_a_session_is_refused() -> None:
    with pytest.raises(SendError, match="fermée"):
        SmtpSender(SMTP, "pw").send(message())
