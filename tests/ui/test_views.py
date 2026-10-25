"""Tests for the dashboard logic of the web interface (pure functions)."""

from datetime import UTC, date, datetime
from pathlib import Path

from gomme.brokers.fields import ProfileField
from gomme.brokers.registry import load_registry
from gomme.brokers.schema import Source
from gomme.legal.statutes import Jurisdiction
from gomme.profile import Profile
from gomme.tracker.db import RequestRecord
from gomme.tracker.state import Status
from gomme.ui.views import board, countdown, field_reason, french_date, selection, sent_items

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


def record(request_id: int, status: Status, sent_on: date | None = None) -> RequestRecord:
    return RequestRecord(
        id=request_id,
        broker_slug="courtier-fictif",
        channel="email",
        jurisdiction=Jurisdiction.GDPR,
        status=status,
        created_at=NOW,
        sent_on=sent_on,
        extended=False,
        reference=None,
        artifact=None,
    )


def test_board_sorts_requests_by_what_to_do() -> None:
    requests = [
        record(1, Status.QUEUED),
        record(2, Status.SENT, date(2026, 10, 1)),
        record(3, Status.SENT, date(2026, 9, 1)),
        record(4, Status.NEEDS_ACTION, date(2026, 10, 1)),
        record(5, Status.OVERDUE, date(2026, 8, 1)),
        record(6, Status.COMPLETED, date(2026, 8, 1)),
        record(7, Status.DRAFT),
    ]

    result = board(requests, {}, NOW.date())

    assert [row.request.id for row in result.to_send] == [1]
    assert [row.request.id for row in result.needs_you] == [4]
    assert [row.request.id for row in result.problems] == [5]
    assert [row.request.id for row in result.waiting] == [3, 2]  # closest deadline first
    assert [row.request.id for row in result.done] == [6]
    assert result.drafts == 1
    assert result.total == 6
    assert result.progress == 17
    assert result.waiting[0].name == "courtier-fictif"  # no definition: the slug


def test_empty_board() -> None:
    result = board([], {}, NOW.date())

    assert result.total == 0
    assert result.progress == 0


def test_selection_counts_missing_fields(fixtures_brokers_dir: Path, profile: Profile) -> None:
    brokers = load_registry(fixtures_brokers_dir)
    no_address = profile.model_copy(update={"postal_address": None})

    assert selection(brokers, profile).ready == 1
    blocked = selection(brokers, no_address)
    assert blocked.ready == 0
    assert blocked.blocked == {ProfileField.POSTAL_ADDRESS: 1}
    assert selection(brokers, profile, already=["courtier-fictif"]).ready == 0


def test_countdown_and_dates() -> None:
    assert countdown(12) == "encore 12 jours"
    assert countdown(1) == "dernier jour demain"
    assert countdown(0) == "dernier jour aujourd'hui"
    assert countdown(-1) == "délai dépassé de 1 jour"
    assert countdown(-3) == "délai dépassé de 3 jours"
    assert french_date(date(2026, 8, 1)) == "1 août 2026"
    assert french_date(None) == "—"


def test_field_reasons_by_source(fixtures_brokers_dir: Path) -> None:
    broker = load_registry(fixtures_brokers_dir)[0]
    sourced = broker.model_copy(
        update={
            "fields_why": {ProfileField.EMAIL: "« Indiquez votre email » (https://example.org)."}
        }
    )
    assert field_reason(sourced, ProfileField.EMAIL).startswith("« Indiquez")
    eraser = broker.model_copy(update={"source": Source.ERASER})
    assert field_reason(eraser, ProfileField.POSTAL_ADDRESS).startswith("Estimation")
    assert "Eraser" in field_reason(eraser, ProfileField.EMAIL)
    optout = broker.model_copy(update={"source": Source.OPTOUT})
    assert "OptOut" in field_reason(optout, ProfileField.FULL_NAME)


def test_sent_items_count_the_values(fixtures_brokers_dir: Path, profile: Profile) -> None:
    broker = next(b for b in load_registry(fixtures_brokers_dir) if b.slug == "courtier-fictif")
    richer = profile.model_copy(update={"other_emails": ("ancienne@example.org",)})

    assert sent_items(broker, profile) == ["nom", "adresse email", "adresse postale"]
    assert sent_items(broker, richer) == ["nom", "2 adresses email", "adresse postale"]
