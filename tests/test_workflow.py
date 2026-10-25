"""Tests for queueing, sending and monitoring requests (fake SMTP, fixed clock)."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fakes import FakePage, FakePrompter, FakeSmtp

from gomme.brokers.schema import Broker
from gomme.channels.email import SmtpSender
from gomme.config import Config, EmailConfig, EmailMode, SmtpConfig
from gomme.legal.statutes import Jurisdiction
from gomme.profile import Profile
from gomme.tracker.db import Tracker
from gomme.tracker.state import Action, Status
from gomme.workflow import (
    ReminderError,
    default_selection,
    monitor,
    queue_requests,
    send_emails,
    send_reminder,
    submit_web_forms,
)

T0 = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)  # a Monday


def broker(slug: str, **changes: Any) -> Broker:
    data: dict[str, Any] = {
        "slug": slug,
        "name": f"{slug} (TEST FIXTURE)",
        "domain": f"{slug}.example",
        "region": "eu",
        "category": "marketing",
        "channels": [{"type": "email", "address": f"dpo@{slug}.example"}],
        "legal_basis": ["gdpr_art17", "gdpr_art21"],
        "required_fields": ["full_name", "email"],
        "requires_id": False,
        "language": "fr",
        "source": "eraser",
        "source_ref": "https://example.org/fixture",
    }
    return Broker.model_validate(data | changes)


@pytest.fixture
def tracker(tmp_path: Path) -> Iterator[Tracker]:
    with Tracker(tmp_path / "gomme.db") as tracker:
        yield tracker


@pytest.fixture
def manual(profile: Profile) -> Config:
    return Config(profile=profile)


@pytest.fixture
def smtp(profile: Profile) -> Config:
    return Config(
        profile=profile,
        email=EmailConfig(
            mode=EmailMode.SMTP,
            smtp=SmtpConfig(host="smtp.example.org", username="jeanne"),
            daily_limit=2,
            delay_seconds=0,
        ),
    )


class Clock:
    def __init__(self, start: datetime) -> None:
        self.current = start

    def __call__(self) -> datetime:
        return self.current


# --- queue ---------------------------------------------------------------------------------


def test_queue_creates_one_request_per_broker(tracker: Tracker, manual: Config) -> None:
    report = queue_requests(tracker, [broker("broker-a"), broker("broker-b")], manual, T0)

    assert report.done == ["broker-a", "broker-b"]
    assert [r.status for r in tracker.requests()] == [Status.QUEUED, Status.QUEUED]
    assert tracker.requests()[0].jurisdiction == "gdpr"


def test_queue_skips_requests_under_way(tracker: Tracker, manual: Config) -> None:
    queue_requests(tracker, [broker("broker-a")], manual, T0)

    report = queue_requests(tracker, [broker("broker-a")], manual, T0)

    assert report.done == []
    assert report.skipped == [("broker-a", "demande n° 1 déjà en cours (queued)")]


def test_queue_skips_brokers_needing_missing_profile_data(tracker: Tracker, manual: Config) -> None:
    report = queue_requests(
        tracker, [broker("broker-a", required_fields=["full_name", "phone"])], manual, T0
    )

    assert report.skipped == [("broker-a", "profil incomplet (phone)")]
    assert tracker.requests() == ()


def test_queue_can_require_a_channel(tracker: Tracker, manual: Config) -> None:
    postal_only = broker("broker-a", channels=[{"type": "postal", "address": "1 rue X"}])

    report = queue_requests(tracker, [postal_only], manual, T0, channel="email")

    assert report.skipped == [("broker-a", "aucun canal email")]


def test_queue_requeues_after_a_recheck(tracker: Tracker, manual: Config) -> None:
    queue_requests(tracker, [broker("broker-a")], manual, T0)
    for action in (Action.SEND, Action.COMPLETE, Action.RECHECK):
        tracker.apply(1, action, T0)

    report = queue_requests(tracker, [broker("broker-a")], manual, T0)

    assert report.done == ["broker-a"]
    assert tracker.get(1).status is Status.QUEUED


# --- send ----------------------------------------------------------------------------------


def test_dry_run_writes_and_sends_nothing(tracker: Tracker, smtp: Config, tmp_path: Path) -> None:
    queue_requests(tracker, [broker("broker-a")], smtp, T0)

    def no_sender() -> SmtpSender:
        raise AssertionError("dry run must not connect")

    report = send_emails(
        tracker,
        {"broker-a": broker("broker-a")},
        smtp,
        Clock(T0),
        outbox=tmp_path / "outbox",
        dry_run=True,
        sender=no_sender,
    )

    assert [item.broker.slug for item in report.previews] == ["broker-a"]
    assert report.previews[0].to == "dpo@broker-a.example"
    assert not (tmp_path / "outbox").exists()
    assert tracker.get(1).status is Status.QUEUED


def test_manual_mode_writes_eml_files_and_waits_for_the_user(
    tracker: Tracker, manual: Config, tmp_path: Path
) -> None:
    queue_requests(tracker, [broker("broker-a")], manual, T0)

    report = send_emails(
        tracker,
        {"broker-a": broker("broker-a")},
        manual,
        Clock(T0),
        outbox=tmp_path / "outbox",
        dry_run=False,
    )

    ((request_id, path, mailto),) = report.files
    assert path == tmp_path / "outbox" / "1-broker-a.eml"
    assert "To: dpo@broker-a.example" in path.read_text(encoding="utf-8")
    assert mailto.startswith("mailto:dpo@broker-a.example")
    assert tracker.get(request_id).status is Status.QUEUED  # sent only once the user says so


def test_smtp_mode_sends_records_and_keeps_a_copy(
    tracker: Tracker, smtp: Config, tmp_path: Path, fake_smtp: FakeSmtp
) -> None:
    brokers = {slug: broker(slug) for slug in ("broker-a", "broker-b")}
    queue_requests(tracker, brokers.values(), smtp, T0)
    assert smtp.email.smtp is not None
    config = smtp.email.smtp

    report = send_emails(
        tracker,
        brokers,
        smtp,
        Clock(T0),
        outbox=tmp_path / "outbox",
        dry_run=False,
        sender=lambda: SmtpSender(config, "pw", factory=lambda _c, _x: fake_smtp),
    )

    assert report.done == ["broker-a", "broker-b"]
    assert [to for _, to in fake_smtp.sent] == [
        ["dpo@broker-a.example", "jeanne.exemple@example.org"],
        ["dpo@broker-b.example", "jeanne.exemple@example.org"],
    ]
    first = tracker.get(1)
    assert first.status is Status.SENT
    assert first.reference == fake_smtp.sent[0][0]["Message-ID"]
    assert first.artifact == str(tmp_path / "outbox" / "sent" / "1-broker-a.eml")
    assert tracker.events(1)[-1].note == "email envoyé à d***@b***.example"


def test_daily_limit_is_respected(
    tracker: Tracker, smtp: Config, tmp_path: Path, fake_smtp: FakeSmtp
) -> None:
    brokers = {slug: broker(slug) for slug in ("broker-a", "broker-b", "broker-c")}
    queue_requests(tracker, brokers.values(), smtp, T0)
    assert smtp.email.smtp is not None
    config = smtp.email.smtp

    def run(clock: Clock) -> list[str]:
        return send_emails(
            tracker,
            brokers,
            smtp,
            clock,
            outbox=tmp_path,
            dry_run=False,
            sender=lambda: SmtpSender(config, "pw", factory=lambda _c, _x: fake_smtp),
        ).done

    assert run(Clock(T0)) == ["broker-a", "broker-b"]  # daily_limit = 2
    assert run(Clock(T0)) == []
    assert run(Clock(T0 + timedelta(days=1))) == ["broker-c"]


def test_smtp_failure_stops_the_batch_and_keeps_requests_queued(
    tracker: Tracker, smtp: Config, tmp_path: Path, fake_smtp: FakeSmtp
) -> None:
    brokers = {"broker-a": broker("broker-a")}
    queue_requests(tracker, brokers.values(), smtp, T0)
    assert smtp.email.smtp is not None
    config = smtp.email.smtp
    fake_smtp.fail_on = "send"

    report = send_emails(
        tracker,
        brokers,
        smtp,
        Clock(T0),
        outbox=tmp_path,
        dry_run=False,
        sender=lambda: SmtpSender(config, "pw", factory=lambda _c, _x: fake_smtp),
    )

    assert len(report.errors) == 1
    assert "envoi refusé" in report.errors[0]
    assert tracker.get(1).status is Status.QUEUED


def test_requests_for_vanished_brokers_are_skipped(
    tracker: Tracker, manual: Config, tmp_path: Path
) -> None:
    queue_requests(tracker, [broker("broker-a")], manual, T0)

    report = send_emails(tracker, {}, manual, Clock(T0), outbox=tmp_path, dry_run=True)

    assert report.skipped == [("broker-a", "fiche broker introuvable")]


# --- monitor ---------------------------------------------------------------------------


def sent_request(tracker: Tracker, manual: Config, when: datetime) -> int:
    queue_requests(tracker, [broker("broker-a")], manual, when)
    tracker.apply(1, Action.SEND, when)
    return 1


def test_monitor_marks_overdue_requests(tracker: Tracker, manual: Config) -> None:
    request_id = sent_request(tracker, manual, T0)  # due Thursday 26 November 2026
    brokers = {"broker-a": broker("broker-a")}

    assert monitor(tracker, brokers, manual, datetime(2026, 11, 26, tzinfo=UTC)).done == []
    report = monitor(tracker, brokers, manual, datetime(2026, 11, 27, tzinfo=UTC))

    assert report.done == ["broker-a : en retard"]
    assert tracker.get(request_id).status is Status.OVERDUE
    assert "2026-11-26" in tracker.events(request_id)[-1].note


def test_monitor_leaves_requests_waiting_for_the_user(tracker: Tracker, manual: Config) -> None:
    request_id = sent_request(tracker, manual, T0)
    tracker.apply(request_id, Action.REQUEST_ACTION, T0)

    monitor(tracker, {}, manual, T0 + timedelta(days=365))

    assert tracker.get(request_id).status is Status.NEEDS_ACTION


def test_monitor_schedules_rechecks(tracker: Tracker, manual: Config) -> None:
    request_id = sent_request(tracker, manual, T0)
    tracker.apply(request_id, Action.COMPLETE, T0)

    monitor(tracker, {}, manual, T0 + timedelta(days=74))
    assert tracker.get(request_id).status is Status.COMPLETED

    report = monitor(tracker, {}, manual, T0 + timedelta(days=75))
    assert report.done == ["broker-a : à re-vérifier"]
    assert tracker.get(request_id).status is Status.RECHECK_DUE


# --- web forms -------------------------------------------------------------------------------

AUTOMATED_STEPS = [
    {"id": "open", "type": "navigate"},
    {
        "id": "fill",
        "type": "form_fill",
        "fields": {"email": {"selector": "#email", "value": "{email}"}},
    },
    {"id": "submit", "type": "click", "selector": "#submit"},
    {"id": "proof", "type": "capture"},
]


def form_broker(steps: list[dict[str, Any]]) -> Broker:
    return broker(
        "broker-form",
        domain="example.org",
        channels=[{"type": "web_form", "url": "https://www.example.org/optout", "steps": steps}],
    )


def submit(
    tracker: Tracker,
    config: Config,
    tmp_path: Path,
    form: Broker,
    page: FakePage,
    *,
    confirmed: bool = True,
) -> tuple[Any, list[str]]:
    opened: list[str] = []

    @contextmanager
    def open_page() -> Iterator[FakePage]:
        yield page

    report = submit_web_forms(
        tracker,
        {form.slug: form},
        config,
        Clock(T0),
        open_page=open_page,
        prompter=FakePrompter(page),
        confirm=lambda _question: confirmed,
        open_url=opened.append,
        proofs_dir=tmp_path / "screenshots",
    )
    return report, opened


def test_automated_form_is_filled_then_confirmed(
    tracker: Tracker, manual: Config, tmp_path: Path
) -> None:
    form = form_broker(AUTOMATED_STEPS)
    queue_requests(tracker, [form], manual, T0)
    page = FakePage()

    report, opened = submit(tracker, manual, tmp_path, form, page)

    assert report.done == ["broker-form"]
    assert opened == []
    assert ("fill", "#email", "jeanne.exemple@example.org", "fill", "None") in page.log
    request = tracker.get(1)
    assert request.status is Status.SENT
    assert request.artifact == str(tmp_path / "screenshots" / "1-broker-form-1.png")


def test_unconfirmed_submission_stays_queued(
    tracker: Tracker, manual: Config, tmp_path: Path
) -> None:
    form = form_broker(AUTOMATED_STEPS)
    queue_requests(tracker, [form], manual, T0)

    report, _ = submit(tracker, manual, tmp_path, form, FakePage(), confirmed=False)

    assert report.skipped == [("broker-form", "envoi non confirmé")]
    assert tracker.get(1).status is Status.QUEUED


def test_failed_form_is_journaled_and_stays_queued(
    tracker: Tracker, manual: Config, tmp_path: Path
) -> None:
    form = form_broker(AUTOMATED_STEPS)
    queue_requests(tracker, [form], manual, T0)
    page = FakePage()
    page.failing.add("#submit")

    report, _ = submit(tracker, manual, tmp_path, form, page)

    assert report.errors == [
        "broker-form : étape submit : TimeoutError (Timeout 15000ms exceeded waiting for #submit)"
    ]
    assert tracker.get(1).status is Status.QUEUED
    assert "échec du formulaire" in tracker.events(1)[-1].note


def test_manual_form_opens_the_page_and_names_the_fields(
    tracker: Tracker, manual: Config, tmp_path: Path
) -> None:
    form = form_broker([])
    queue_requests(tracker, [form], manual, T0)
    page = FakePage()

    report, opened = submit(tracker, manual, tmp_path, form, page)

    assert opened == ["https://www.example.org/optout"]
    assert report.done == ["broker-form"]
    assert tracker.events(1)[-1].note == "formulaire rempli à la main"


# --- reminders -------------------------------------------------------------------------------


def overdue(tracker: Tracker, manual: Config) -> int:
    request_id = sent_request(tracker, manual, T0)
    tracker.apply(request_id, Action.MARK_OVERDUE, T0 + timedelta(days=40))
    return request_id


def test_reminder_is_written_in_manual_mode(
    tracker: Tracker, manual: Config, tmp_path: Path
) -> None:
    request_id = overdue(tracker, manual)

    report = send_reminder(
        tracker,
        {"broker-a": broker("broker-a")},
        manual,
        Clock(T0 + timedelta(days=41)),
        request_id,
        outbox=tmp_path / "outbox",
        dry_run=False,
    )

    assert report.text.subject.startswith(
        "Relance : demande d'effacement et d'opposition du 26/10/2026"
    )
    assert "a expiré le 26/11/2026 sans réponse" in report.text.body
    assert report.path == tmp_path / "outbox" / "1-broker-a-relance-1.eml"
    assert tracker.get(request_id).status is Status.OVERDUE
    assert tracker.events(request_id)[-1].note.startswith("relance préparée")


def test_reminder_numbers_increase_and_mention_acknowledgement(
    tracker: Tracker, manual: Config, tmp_path: Path
) -> None:
    request_id = sent_request(tracker, manual, T0)
    tracker.apply(request_id, Action.ACKNOWLEDGE, T0)
    tracker.apply(request_id, Action.MARK_OVERDUE, T0 + timedelta(days=40))
    brokers = {"broker-a": broker("broker-a")}

    def remind() -> Any:
        return send_reminder(
            tracker, brokers, manual, Clock(T0), request_id, outbox=tmp_path, dry_run=False
        )

    remind()
    second = remind()

    assert second.path == tmp_path / "1-broker-a-relance-2.eml"
    assert "aucune réponse définitive" in second.text.body


def test_smtp_reminder_joins_the_original_thread(
    tracker: Tracker, smtp: Config, tmp_path: Path, fake_smtp: FakeSmtp
) -> None:
    queue_requests(tracker, [broker("broker-a")], smtp, T0)
    tracker.apply(1, Action.SEND, T0, reference="<original@example.org>")
    tracker.apply(1, Action.MARK_OVERDUE, T0 + timedelta(days=40))
    assert smtp.email.smtp is not None
    config = smtp.email.smtp

    report = send_reminder(
        tracker,
        {"broker-a": broker("broker-a")},
        smtp,
        Clock(T0 + timedelta(days=41)),
        1,
        outbox=tmp_path,
        dry_run=False,
        sender=lambda: SmtpSender(config, "pw", factory=lambda _c, _x: fake_smtp),
    )

    assert report.sent
    ((message, _),) = fake_smtp.sent
    assert message["In-Reply-To"] == "<original@example.org>"
    assert tracker.events(1)[-1].note == "relance envoyée à d***@b***.example"


def test_broker_without_email_gets_a_letter(
    tracker: Tracker, manual: Config, tmp_path: Path
) -> None:
    postal = broker(
        "broker-a", channels=[{"type": "postal", "address": "Broker A, 1 rue X, 00000 Y"}]
    )
    request_id = overdue(tracker, manual)

    report = send_reminder(
        tracker,
        {"broker-a": postal},
        manual,
        Clock(T0),
        request_id,
        outbox=tmp_path / "outbox",
        dry_run=False,
    )

    assert report.to is None
    assert report.path == tmp_path / "letters" / "1-broker-a-relance-1.pdf"
    assert report.path.read_bytes().startswith(b"%PDF")


def test_only_overdue_requests_are_reminded(
    tracker: Tracker, manual: Config, tmp_path: Path
) -> None:
    request_id = sent_request(tracker, manual, T0)

    with pytest.raises(ReminderError, match="gomme monitor"):
        send_reminder(
            tracker,
            {"broker-a": broker("broker-a")},
            manual,
            Clock(T0),
            request_id,
            outbox=tmp_path,
            dry_run=True,
        )


def test_english_ccpa_reminder(tracker: Tracker, manual: Config, tmp_path: Path) -> None:
    us = broker("broker-a", legal_basis=["ccpa_1798_105"], language="en")
    request = tracker.create("broker-a", "email", Jurisdiction.CCPA, T0)
    for action in (Action.QUEUE, Action.SEND, Action.MARK_OVERDUE):
        tracker.apply(request.id, action, T0)

    report = send_reminder(
        tracker, {"broker-a": us}, manual, Clock(T0), request.id, outbox=tmp_path, dry_run=True
    )

    assert (
        report.text.subject
        == "Reminder: request to delete personal information of October 26, 2026"
    )
    assert "I have not received a final answer" in report.text.body


def test_refused_broker_address_is_not_marked_sent_and_the_queue_goes_on(
    tracker: Tracker, smtp: Config, tmp_path: Path, fake_smtp: FakeSmtp
) -> None:
    brokers = {slug: broker(slug) for slug in ("broker-a", "broker-b")}
    queue_requests(tracker, brokers.values(), smtp, T0)
    assert smtp.email.smtp is not None
    config = smtp.email.smtp
    fake_smtp.refuse.add("dpo@broker-a.example")  # the user's copy is still accepted

    report = send_emails(
        tracker,
        brokers,
        smtp,
        Clock(T0),
        outbox=tmp_path,
        dry_run=False,
        sender=lambda: SmtpSender(config, "pw", factory=lambda _c, _x: fake_smtp),
    )

    assert report.done == ["broker-b"]
    assert report.errors == [
        "n° 1 (broker-a) : adresse du broker refusée par smtp.example.org (code 550)"
    ]
    assert tracker.get(1).status is Status.QUEUED
    assert tracker.get(2).status is Status.SENT
    assert tracker.events(1)[-1].note.startswith("envoi impossible")


def test_fully_refused_message_is_per_request_too(
    tracker: Tracker, smtp: Config, tmp_path: Path, fake_smtp: FakeSmtp
) -> None:
    brokers = {slug: broker(slug) for slug in ("broker-a", "broker-b")}
    queue_requests(tracker, brokers.values(), smtp, T0)
    assert smtp.email.smtp is not None
    config = smtp.email.smtp
    fake_smtp.refuse |= {"dpo@broker-a.example", "jeanne.exemple@example.org"}

    report = send_emails(
        tracker,
        brokers,
        smtp,
        Clock(T0),
        outbox=tmp_path,
        dry_run=False,
        sender=lambda: SmtpSender(config, "pw", factory=lambda _c, _x: fake_smtp),
    )

    assert tracker.get(1).status is Status.QUEUED
    assert len(report.errors) == 1
    # broker-b's own copy to the user is refused too, but the broker got it: sent.
    assert tracker.get(2).status is Status.SENT


def test_manual_sends_do_not_use_up_the_smtp_quota(
    tracker: Tracker, smtp: Config, tmp_path: Path, fake_smtp: FakeSmtp
) -> None:
    brokers = {slug: broker(slug) for slug in ("broker-a", "broker-b", "broker-c")}
    queue_requests(tracker, brokers.values(), smtp, T0)
    tracker.apply(1, Action.SEND, T0)  # sent by hand (mark send), daily_limit = 2
    assert smtp.email.smtp is not None
    config = smtp.email.smtp

    report = send_emails(
        tracker,
        brokers,
        smtp,
        Clock(T0),
        outbox=tmp_path,
        dry_run=False,
        sender=lambda: SmtpSender(config, "pw", factory=lambda _c, _x: fake_smtp),
    )

    assert report.done == ["broker-b", "broker-c"]


def test_web_form_only_broker_cannot_be_reminded_by_email_or_letter(
    tracker: Tracker, manual: Config, tmp_path: Path
) -> None:
    form = form_broker([])
    queue_requests(tracker, [form], manual, T0)
    tracker.apply(1, Action.SEND, T0)
    tracker.apply(1, Action.MARK_OVERDUE, T0 + timedelta(days=40))

    with pytest.raises(ReminderError, match=r"formulaire : https://www\.example\.org/optout"):
        send_reminder(
            tracker, {form.slug: form}, manual, Clock(T0), 1, outbox=tmp_path, dry_run=False
        )


def test_extended_deadline_is_stated_in_the_reminder(
    tracker: Tracker, manual: Config, tmp_path: Path
) -> None:
    request_id = sent_request(tracker, manual, T0)
    tracker.apply(request_id, Action.EXTEND, T0)
    tracker.apply(request_id, Action.MARK_OVERDUE, T0 + timedelta(days=100))

    report = send_reminder(
        tracker,
        {"broker-a": broker("broker-a")},
        manual,
        Clock(T0),
        request_id,
        outbox=tmp_path,
        dry_run=True,
    )

    assert "prolongé de deux mois" in report.text.body
    assert "a expiré le 26/01/2027" in report.text.body


def test_default_selection_follows_the_users_market(profile: Profile) -> None:
    brokers = [
        broker("fr-broker", region="fr", country="FR"),
        broker("de-bureau", region="eu", country="DE"),
        broker("worldwide", region="global"),
        broker("us-site", region="us"),
    ]

    def slugs(country: str) -> list[str]:
        return [
            b.slug
            for b in default_selection(brokers, profile.model_copy(update={"country": country}))[1]
        ]

    assert slugs("FR") == ["fr-broker", "worldwide"]  # not the German credit bureau
    assert slugs("DE") == ["de-bureau", "worldwide"]
    assert slugs("US") == ["worldwide", "us-site"]
    assert slugs("JP") == ["worldwide"]
