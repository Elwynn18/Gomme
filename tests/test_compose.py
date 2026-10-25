"""Tests for the text of removal requests."""

from pathlib import Path
from typing import Any

import pytest

from gomme.brokers.registry import load_broker_file
from gomme.brokers.schema import Broker
from gomme.compose import choose_jurisdiction, compose
from gomme.legal.statutes import Jurisdiction
from gomme.profile import MissingProfileDataError, PostalAddress, Profile


@pytest.fixture
def french_broker(fixtures_brokers_dir: Path) -> Broker:
    return load_broker_file(fixtures_brokers_dir / "fr" / "courtier-fictif.yml")


def broker(**changes: Any) -> Broker:
    data: dict[str, Any] = {
        "slug": "fake-broker",
        "name": "Fake Broker (TEST FIXTURE)",
        "domain": "example.org",
        "region": "us",
        "category": "marketing",
        "channels": [{"type": "email", "address": "privacy@example.org"}],
        "legal_basis": ["ccpa_1798_105"],
        "required_fields": ["full_name", "email"],
        "requires_id": False,
        "language": "en",
        "source": "eraser",
        "source_ref": "https://example.org/fixture",
    }
    return Broker.model_validate(data | changes)


def californian(profile: Profile) -> Profile:
    address = PostalAddress(
        street="1 Example St", postal_code="00000", city="Nowhere", country="US", state="CA"
    )
    return profile.model_copy(update={"country": "US", "language": "en", "postal_address": address})


# --- Which law ------------------------------------------------------------------------


def test_european_resident_uses_gdpr_when_available(profile: Profile) -> None:
    global_broker = broker(legal_basis=["gdpr_art17", "gdpr_art21", "ccpa_1798_105"])

    assert choose_jurisdiction(profile, global_broker) is Jurisdiction.GDPR
    assert choose_jurisdiction(californian(profile), global_broker) is Jurisdiction.CCPA


def test_us_only_broker_gets_a_ccpa_request(profile: Profile) -> None:
    assert choose_jurisdiction(profile, broker()) is Jurisdiction.CCPA


def test_european_broker_gets_a_gdpr_request_from_anyone(
    profile: Profile, french_broker: Broker
) -> None:
    assert choose_jurisdiction(californian(profile), french_broker) is Jurisdiction.GDPR


# --- GDPR text --------------------------------------------------------------------------


def test_gdpr_request_combines_erasure_and_objection(
    profile: Profile, french_broker: Broker
) -> None:
    text = compose(french_broker, profile)

    assert text.language == "fr"
    assert text.subject == "Demande d'effacement et d'opposition (articles 17 et 21 du RGPD)"
    assert "article 21" in text.body
    assert "article 17" in text.body
    assert "je retire ce consentement (article 7, paragraphe 3)" in text.body
    assert "un mois" in text.body
    assert "de la CNIL" in text.body
    assert "je ne joins pas de pièce d'identité" in text.body
    assert text.body.rstrip().endswith("Jeanne Exemple")


def test_optional_articles(profile: Profile, french_broker: Broker) -> None:
    default = compose(french_broker, profile).body
    full = compose(french_broker, profile, access=True, notify_recipients=True).body
    bare = compose(french_broker, profile, notify_recipients=False).body

    assert "article 15" not in default
    assert "article 19" in default
    assert "article 15" in full
    assert "article 19" not in bare


def test_only_required_fields_identify_the_user(profile: Profile) -> None:
    profile = profile.model_copy(update={"phone": "06 00 00 00 00"})

    body = compose(broker(required_fields=["full_name", "email"]), profile).body

    assert "jeanne.exemple@example.org" in body
    assert "rue de l'Exemple" not in body
    assert "06 00" not in body


def test_missing_profile_data_stops_the_request(profile: Profile) -> None:
    with pytest.raises(MissingProfileDataError, match="birth_date"):
        compose(broker(required_fields=["full_name", "birth_date"]), profile)


def test_english_gdpr_request_for_an_english_broker(profile: Profile) -> None:
    text = compose(broker(legal_basis=["gdpr_art17", "gdpr_art21"]), profile)

    assert text.language == "en"
    assert text.subject.startswith("Request for erasure and objection")
    assert "the CNIL" in text.body


def test_other_european_residents_name_the_competent_authority(profile: Profile) -> None:
    german = profile.model_copy(update={"country": "DE"})

    body = compose(broker(legal_basis=["gdpr_art17", "gdpr_art21"]), german).body

    assert "the competent supervisory authority" in body


def test_french_broker_without_french_template_falls_back_to_english(profile: Profile) -> None:
    text = compose(broker(language="fr"), californian(profile))  # CCPA exists in English only

    assert (text.language, text.jurisdiction) == ("en", Jurisdiction.CCPA)


# --- CCPA text --------------------------------------------------------------------------


def test_ccpa_request_claims_residency_only_for_californians(profile: Profile) -> None:
    resident = compose(broker(), californian(profile)).body
    other = compose(broker(), profile).body

    assert "I am a California resident" in resident
    assert "45 calendar days" in resident
    assert "California resident" not in other.replace("California residents", "")
    assert "45 calendar days" not in other
    assert "§1798.105" in other
