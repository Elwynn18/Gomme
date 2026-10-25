"""Tests for the CNIL complaint draft."""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from gomme.brokers.registry import load_broker_file
from gomme.brokers.schema import Broker
from gomme.legal.complaint import ComplaintError, compose_complaint
from gomme.legal.statutes import Jurisdiction
from gomme.profile import Profile
from gomme.tracker.db import Tracker
from gomme.tracker.state import Action

SENT = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
LATER = datetime(2026, 12, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def broker(fixtures_brokers_dir: Path) -> Broker:
    return load_broker_file(fixtures_brokers_dir / "fr" / "courtier-fictif.yml")


@pytest.fixture
def tracker(tmp_path: Path) -> Iterator[Tracker]:
    with Tracker(tmp_path / "gomme.db") as tracker:
        yield tracker


def sent(tracker: Tracker, jurisdiction: Jurisdiction = Jurisdiction.GDPR) -> int:
    request = tracker.create("courtier-fictif", "email", jurisdiction, SENT)
    tracker.apply(request.id, Action.QUEUE, SENT)
    tracker.apply(request.id, Action.SEND, SENT, artifact="/home/x/sent/1-courtier-fictif.eml")
    return request.id


def draft(tracker: Tracker, broker: Broker, profile: Profile, request_id: int) -> str:
    return compose_complaint(
        broker, profile, tracker.get(request_id), tracker.events(request_id), LATER.date()
    )


def test_unanswered_request(tracker: Tracker, broker: Broker, profile: Profile) -> None:
    request_id = sent(tracker)
    tracker.apply(request_id, Action.MARK_OVERDUE, LATER)

    text = draft(tracker, broker, profile, request_id)

    assert "Absence de réponse de Courtier Fictif" in text
    assert "Le 26/10/2026, j'ai adressé" in text
    assert "par email" in text
    assert "soit jusqu'au 26/11/2026" in text
    assert "aucune réponse" in text
    assert "dpo@example.org" in text
    assert "SIREN : [à compléter" in text
    assert "/home/x/sent/1-courtier-fictif.eml" in text
    assert "https://www.cnil.fr/fr/adresser-une-plainte" in text
    assert "délai légal de réponse dépassé" in text


def test_refused_request(tracker: Tracker, broker: Broker, profile: Profile) -> None:
    request_id = sent(tracker)
    tracker.apply(request_id, Action.REFUSE, LATER, note="intérêt légitime invoqué")

    text = draft(tracker, broker, profile, request_id)

    assert text.count("Refus de Courtier Fictif") == 1
    assert "a refusé de donner suite à cette demande le 01/12/2026" in text
    assert "intérêt légitime invoqué" in text
    assert "contesté auprès de l'organisme" in text


def test_extension_is_mentioned(tracker: Tracker, broker: Broker, profile: Profile) -> None:
    request_id = sent(tracker)
    tracker.apply(request_id, Action.EXTEND, SENT)
    tracker.apply(request_id, Action.MARK_OVERDUE, datetime(2027, 2, 1, tzinfo=UTC))

    text = compose_complaint(
        broker, profile, tracker.get(request_id), tracker.events(request_id), date(2027, 2, 1)
    )

    assert "prolongé de deux mois" in text
    assert "jusqu'au 26/01/2027" in text
    assert "malgré un accusé de réception" not in text


def test_only_late_or_refused_requests(tracker: Tracker, broker: Broker, profile: Profile) -> None:
    request_id = sent(tracker)

    with pytest.raises(ComplaintError, match="en retard ou refusée"):
        draft(tracker, broker, profile, request_id)


def test_only_gdpr_requests(tracker: Tracker, broker: Broker, profile: Profile) -> None:
    request_id = sent(tracker, Jurisdiction.CCPA)
    tracker.apply(request_id, Action.MARK_OVERDUE, LATER)

    with pytest.raises(ComplaintError, match="RGPD"):
        draft(tracker, broker, profile, request_id)
