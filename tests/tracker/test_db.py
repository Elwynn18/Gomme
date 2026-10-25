"""Tests for the request database and its journal."""

import sqlite3
import stat
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from gomme.legal.statutes import Jurisdiction
from gomme.tracker.db import CREATE, Tracker, TrackerError
from gomme.tracker.state import Action, InvalidTransitionError, Status

T0 = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)


@pytest.fixture
def tracker(tmp_path: Path) -> Iterator[Tracker]:
    with Tracker(tmp_path / "data" / "gomme.db") as tracker:
        yield tracker


def test_database_file_is_private(tmp_path: Path) -> None:
    path = tmp_path / "data" / "gomme.db"
    Tracker(path).close()

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_new_request_is_a_journaled_draft(tracker: Tracker) -> None:
    request = tracker.create("courtier-fictif", "email", Jurisdiction.GDPR, T0)

    assert request.status is Status.DRAFT
    assert request.sent_on is None
    assert request.deadline() is None
    (event,) = tracker.events(request.id)
    assert (event.action, event.from_status, event.to_status) == (CREATE, None, Status.DRAFT)


def test_every_transition_is_journaled(tracker: Tracker) -> None:
    request = tracker.create("courtier-fictif", "email", Jurisdiction.GDPR, T0)

    tracker.apply(request.id, Action.QUEUE, T0)
    sent = tracker.apply(request.id, Action.SEND, T0, reference="<id@example.org>")
    tracker.apply(request.id, Action.ACKNOWLEDGE, T0, note="accusé de réception")

    assert sent.sent_on == date(2026, 10, 25)
    assert sent.reference == "<id@example.org>"
    assert [event.action for event in tracker.events(request.id)] == [
        CREATE,
        "queue",
        "send",
        "acknowledge",
    ]
    assert tracker.events(request.id)[-1].note == "accusé de réception"


def test_invalid_action_changes_nothing(tracker: Tracker) -> None:
    request = tracker.create("courtier-fictif", "email", Jurisdiction.GDPR, T0)

    with pytest.raises(InvalidTransitionError):
        tracker.apply(request.id, Action.SEND, T0)

    assert tracker.get(request.id).status is Status.DRAFT
    assert len(tracker.events(request.id)) == 1


def test_deadline_follows_sending_and_extension(tracker: Tracker) -> None:
    request = tracker.create("courtier-fictif", "email", Jurisdiction.GDPR, T0)
    tracker.apply(request.id, Action.QUEUE, T0)
    sent = tracker.apply(request.id, Action.SEND, T0)

    deadline = sent.deadline()
    assert deadline is not None
    assert deadline.nominal == date(2026, 11, 25)

    extended = tracker.apply(request.id, Action.EXTEND, T0).deadline()
    assert extended is not None
    assert extended.nominal == date(2027, 1, 25)


def test_resending_after_a_recheck_resets_the_deadline(tracker: Tracker) -> None:
    request = tracker.create("courtier-fictif", "email", Jurisdiction.CCPA, T0)
    for action in (Action.QUEUE, Action.SEND, Action.EXTEND, Action.COMPLETE, Action.RECHECK):
        tracker.apply(request.id, action, T0)

    queued = tracker.apply(request.id, Action.QUEUE, T0)

    assert (queued.status, queued.sent_on, queued.extended) == (Status.QUEUED, None, False)


def test_queries(tracker: Tracker) -> None:
    first = tracker.create("broker-a", "email", Jurisdiction.GDPR, T0)
    tracker.create("broker-b", "web_form", Jurisdiction.CCPA, T0)
    second = tracker.create("broker-a", "email", Jurisdiction.GDPR, T0)
    tracker.apply(first.id, Action.QUEUE, T0)
    tracker.apply(first.id, Action.SEND, T0)

    assert [r.id for r in tracker.requests(status=Status.SENT)] == [first.id]
    assert [r.id for r in tracker.requests(broker_slug="broker-a")] == [first.id, second.id]
    latest = tracker.latest_for("broker-a")
    assert latest is not None
    assert latest.id == second.id
    assert tracker.latest_for("unknown") is None
    tracker.note(first.id, T0, "email envoyé à x")
    tracker.note(first.id, T0, "50%_ off: not a prefix match")
    assert tracker.notes_on(date(2026, 10, 25), ("email envoyé à",)) == 1
    assert tracker.notes_on(date(2026, 10, 25), ("50%",)) == 1
    assert tracker.notes_on(date(2026, 10, 25), ("5_",)) == 0  # _ is literal, not a wildcard
    assert tracker.notes_on(date(2026, 10, 26), ("email envoyé à",)) == 0
    assert tracker.notes_on(date(2026, 10, 25), ()) == 0


def test_notes_do_not_change_the_status(tracker: Tracker) -> None:
    request = tracker.create("broker-a", "email", Jurisdiction.GDPR, T0)

    tracker.note(request.id, T0, "relance téléphonique")

    assert tracker.get(request.id).status is Status.DRAFT
    assert tracker.events(request.id)[-1].note == "relance téléphonique"


def test_unknown_request(tracker: Tracker) -> None:
    with pytest.raises(TrackerError, match="introuvable"):
        tracker.get(42)


def test_database_survives_reopening(tmp_path: Path) -> None:
    path = tmp_path / "gomme.db"
    with Tracker(path) as tracker:
        request = tracker.create("broker-a", "email", Jurisdiction.GDPR, T0)

    with Tracker(path) as tracker:
        assert tracker.get(request.id).broker_slug == "broker-a"


def test_unknown_schema_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "gomme.db"
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version = 99")
    db.close()

    with pytest.raises(TrackerError, match="version 99"):
        Tracker(path)


def test_channel_and_jurisdiction_are_checked_by_the_database(tracker: Tracker) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        tracker.create("broker-a", "fax", Jurisdiction.GDPR, T0)


def test_a_manual_send_can_be_dated(tracker: Tracker) -> None:
    request = tracker.create("broker-a", "email", Jurisdiction.GDPR, T0)
    tracker.apply(request.id, Action.QUEUE, T0)

    sent = tracker.apply(request.id, Action.SEND, T0, sent_on=date(2026, 10, 20))

    assert sent.sent_on == date(2026, 10, 20)


@pytest.mark.parametrize(
    ("action", "day"), [(Action.SEND, date(2026, 10, 26)), (Action.UNQUEUE, date(2026, 10, 20))]
)
def test_send_date_must_be_a_past_send(tracker: Tracker, action: Action, day: date) -> None:
    request = tracker.create("broker-a", "email", Jurisdiction.GDPR, T0)
    tracker.apply(request.id, Action.QUEUE, T0)

    with pytest.raises(TrackerError, match=r"futur|ne valent que pour send"):
        tracker.apply(request.id, action, T0, sent_on=day)


def test_letter_deadline_starts_on_receipt(tracker: Tracker) -> None:
    request = tracker.create("broker-a", "postal", Jurisdiction.GDPR, T0)
    tracker.apply(request.id, Action.QUEUE, T0)

    sent = tracker.apply(
        request.id, Action.SEND, T0, sent_on=date(2026, 10, 19), received_on=date(2026, 10, 22)
    )

    deadline = sent.deadline()
    assert deadline is not None
    assert deadline.nominal == date(2026, 11, 22)


@pytest.mark.parametrize(
    "received",
    [date(2026, 10, 18), date(2026, 10, 26)],  # before sending, in the future
)
def test_receipt_date_must_be_plausible(tracker: Tracker, received: date) -> None:
    request = tracker.create("broker-a", "postal", Jurisdiction.GDPR, T0)
    tracker.apply(request.id, Action.QUEUE, T0)

    with pytest.raises(TrackerError, match="réception"):
        tracker.apply(request.id, Action.SEND, T0, sent_on=date(2026, 10, 19), received_on=received)


def test_waiting_for_the_user_suspends_the_gdpr_deadline(tracker: Tracker) -> None:
    request = tracker.create("broker-a", "email", Jurisdiction.GDPR, T0)
    tracker.apply(request.id, Action.QUEUE, T0)
    tracker.apply(request.id, Action.SEND, T0)  # 25 October: due 25 November
    tracker.apply(request.id, Action.REQUEST_ACTION, T0 + timedelta(days=5))
    done = tracker.apply(request.id, Action.ACTION_DONE, T0 + timedelta(days=15))

    assert done.suspended_days == 10
    deadline = done.deadline()
    assert deadline is not None
    assert deadline.nominal == date(2026, 12, 5)


def test_verification_does_not_suspend_the_ccpa_deadline(tracker: Tracker) -> None:
    request = tracker.create("broker-a", "email", Jurisdiction.CCPA, T0)
    tracker.apply(request.id, Action.QUEUE, T0)
    tracker.apply(request.id, Action.SEND, T0)
    tracker.apply(request.id, Action.REQUEST_ACTION, T0)
    done = tracker.apply(request.id, Action.ACTION_DONE, T0 + timedelta(days=10))

    deadline = done.deadline()
    assert deadline is not None
    assert deadline.nominal == date(2026, 12, 9)  # 25 October + 45 days


def test_version_1_database_is_upgraded(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE requests (id INTEGER PRIMARY KEY, broker_slug TEXT NOT NULL,
            channel TEXT NOT NULL, jurisdiction TEXT NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL, sent_on TEXT, extended INTEGER NOT NULL DEFAULT 0,
            reference TEXT, artifact TEXT);
        CREATE TABLE events (id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL,
            at TEXT NOT NULL, action TEXT NOT NULL, from_status TEXT, to_status TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '');
        INSERT INTO requests VALUES (1, 'broker-a', 'email', 'gdpr', 'sent',
            '2026-10-01T09:00:00+00:00', '2026-10-01', 0, NULL, NULL);
        PRAGMA user_version = 1;
        """
    )
    db.close()

    with Tracker(path) as tracker:
        request = tracker.get(1)

    assert (request.received_on, request.suspended_days) == (None, 0)
    assert request.sent_on == date(2026, 10, 1)


def test_receipt_recorded_after_sending_moves_the_deadline(tracker: Tracker) -> None:
    request = tracker.create("broker-a", "postal", Jurisdiction.GDPR, T0)
    tracker.apply(request.id, Action.QUEUE, T0)
    tracker.apply(request.id, Action.SEND, T0, sent_on=date(2026, 10, 19))
    later = T0 + timedelta(days=1)

    updated = tracker.record_receipt(request.id, date(2026, 10, 22), later)

    deadline = updated.deadline()
    assert deadline is not None
    assert deadline.nominal == date(2026, 11, 22)
    assert "reçue par le broker le 2026-10-22" in tracker.events(request.id)[-1].note


def test_corrected_receipt_resumes_an_overdue_request(tracker: Tracker) -> None:
    request = tracker.create("broker-a", "postal", Jurisdiction.GDPR, T0)
    tracker.apply(request.id, Action.QUEUE, T0)
    tracker.apply(request.id, Action.SEND, T0, sent_on=date(2026, 9, 20))
    tracker.apply(request.id, Action.MARK_OVERDUE, T0)  # due 20 October, posting date
    now = datetime(2026, 10, 26, tzinfo=UTC)

    updated = tracker.record_receipt(request.id, date(2026, 9, 28), now)

    assert updated.status is Status.SENT  # due 28 October
    events = tracker.events(request.id)
    assert (events[-1].action, events[-1].from_status) == ("resume", Status.OVERDUE)


def test_receipt_only_for_sent_requests(tracker: Tracker) -> None:
    request = tracker.create("broker-a", "postal", Jurisdiction.GDPR, T0)

    with pytest.raises(TrackerError, match="envoyée"):
        tracker.record_receipt(request.id, date(2026, 10, 20), T0)


def test_huge_request_numbers_are_not_found(tracker: Tracker) -> None:
    with pytest.raises(TrackerError, match="introuvable"):
        tracker.get(2**70)
