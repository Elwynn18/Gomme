"""Tests for the broker definition schema."""

from datetime import date
from pathlib import Path
from typing import Any

import pydantic
import pytest
import yaml

from gomme.brokers.schema import (
    Broker,
    Category,
    EmailChannel,
    LegalBasis,
    PostalChannel,
    ProfileField,
    Region,
    Source,
    WebFormChannel,
)


def valid_data() -> dict[str, Any]:
    """A fresh, valid and entirely fictional broker definition."""
    return {
        "slug": "courtier-fictif",
        "name": "Courtier Fictif",
        "domain": "example.org",
        "country": "FR",
        "region": "fr",
        "category": "marketing",
        "channels": [{"type": "email", "address": "dpo@example.org"}],
        "legal_basis": ["gdpr_art17", "gdpr_art21"],
        "required_fields": ["full_name", "email"],
        "requires_id": False,
        "language": "fr",
        "source": "manual",
        "source_ref": "https://example.org/source",
        "last_verified": date(2026, 10, 24),
        "notes": "",
    }


def error_fields(exc: pydantic.ValidationError) -> set[tuple[str | int, ...]]:
    return {tuple(err["loc"]) for err in exc.errors()}


# --- Valid definitions -------------------------------------------------------


def test_valid_definition_is_parsed() -> None:
    broker = Broker.model_validate(valid_data())

    assert broker.slug == "courtier-fictif"
    assert broker.region is Region.FR
    assert broker.category is Category.MARKETING
    assert broker.legal_basis == (LegalBasis.GDPR_ART17, LegalBasis.GDPR_ART21)
    assert broker.required_fields == (ProfileField.FULL_NAME, ProfileField.EMAIL)
    assert broker.source is Source.MANUAL
    assert broker.last_verified == date(2026, 10, 24)
    assert broker.channels == (EmailChannel(type="email", address="dpo@example.org"),)


@pytest.mark.parametrize("relative_path", ["fr/courtier-fictif.yml", "us/fake-people-search.yml"])
def test_fixture_files_are_valid(fixtures_brokers_dir: Path, relative_path: str) -> None:
    raw = yaml.safe_load((fixtures_brokers_dir / relative_path).read_text(encoding="utf-8"))

    broker = Broker.model_validate(raw)

    assert broker.domain == "example.org"


def test_notes_default_to_empty() -> None:
    data = valid_data()
    del data["notes"]

    assert Broker.model_validate(data).notes == ""


def test_all_channel_types_are_supported() -> None:
    data = valid_data()
    data["channels"] = [
        {"type": "email", "address": "dpo@example.org"},
        {"type": "web_form", "url": "https://example.org/rgpd"},
        {"type": "postal", "address": "DPO, 1 rue de l'Exemple, 00000 Nulle-Part"},
    ]

    broker = Broker.model_validate(data)

    assert [type(channel) for channel in broker.channels] == [
        EmailChannel,
        WebFormChannel,
        PostalChannel,
    ]


def test_web_form_may_have_no_steps() -> None:
    data = valid_data()
    data["channels"] = [{"type": "web_form", "url": "https://example.org/rgpd", "steps": []}]

    channel = Broker.model_validate(data).channels[0]

    assert isinstance(channel, WebFormChannel)
    assert channel.steps == ()


def test_broker_is_immutable() -> None:
    broker = Broker.model_validate(valid_data())

    with pytest.raises(pydantic.ValidationError):
        broker.slug = "autre-slug"  # type: ignore[misc]


# --- Strictness: unknown fields and channels --------------------------------


def test_unknown_top_level_field_is_rejected() -> None:
    data = valid_data()
    data["phone"] = "0000"

    with pytest.raises(pydantic.ValidationError) as exc_info:
        Broker.model_validate(data)

    assert ("phone",) in error_fields(exc_info.value)


def test_unknown_channel_field_is_rejected() -> None:
    data = valid_data()
    data["channels"] = [{"type": "email", "address": "dpo@example.org", "cc": "x@example.org"}]

    with pytest.raises(pydantic.ValidationError):
        Broker.model_validate(data)


@pytest.mark.parametrize("channels", [[], None])
def test_at_least_one_channel_is_required(channels: list[Any] | None) -> None:
    data = valid_data()
    data["channels"] = channels

    with pytest.raises(pydantic.ValidationError):
        Broker.model_validate(data)


def test_invalid_channels_only_report_their_own_errors() -> None:
    """A broken channel must not also trigger a misleading 'at least one channel' error."""
    data = valid_data()
    data["channels"] = [{"type": "email", "address": "invalide"}]

    with pytest.raises(pydantic.ValidationError) as exc_info:
        Broker.model_validate(data)

    assert ("channels",) not in error_fields(exc_info.value)


def test_unknown_channel_type_is_rejected() -> None:
    data = valid_data()
    data["channels"] = [{"type": "fax", "number": "0000"}]

    with pytest.raises(pydantic.ValidationError):
        Broker.model_validate(data)


@pytest.mark.parametrize(
    "field",
    [
        "slug",
        "name",
        "domain",
        "region",
        "category",
        "channels",
        "legal_basis",
        "required_fields",
        "requires_id",
        "language",
        "source",
        "source_ref",
    ],
)
def test_required_fields_are_enforced(field: str) -> None:
    data = valid_data()
    del data[field]

    with pytest.raises(pydantic.ValidationError) as exc_info:
        Broker.model_validate(data)

    assert (field,) in error_fields(exc_info.value)


# --- Field formats ----------------------------------------------------------


@pytest.mark.parametrize("slug", ["a", "abc", "data-broker-2", "123", "a1-b2-c3"])
def test_kebab_case_slugs_are_accepted(slug: str) -> None:
    data = valid_data()
    data["slug"] = slug

    assert Broker.model_validate(data).slug == slug


@pytest.mark.parametrize(
    "slug", ["", "Abc", "data_broker", "-abc", "abc-", "a--b", "a b", "été", "a.b"]
)
def test_non_kebab_case_slugs_are_rejected(slug: str) -> None:
    data = valid_data()
    data["slug"] = slug

    with pytest.raises(pydantic.ValidationError):
        Broker.model_validate(data)


@pytest.mark.parametrize("country", ["FR", "US", "DE", "NO"])
def test_iso_alpha2_countries_are_accepted(country: str) -> None:
    data = valid_data()
    data["country"] = country

    assert Broker.model_validate(data).country == country


@pytest.mark.parametrize("country", ["", "fr", "FRA", "F", "F1", "Fr", False])
def test_invalid_countries_are_rejected(country: object) -> None:
    data = valid_data()
    data["country"] = country

    with pytest.raises(pydantic.ValidationError):
        Broker.model_validate(data)


@pytest.mark.parametrize("domain", ["example.org", "sub.example.org", "exemple-1.fr"])
def test_valid_domains_are_accepted(domain: str) -> None:
    data = valid_data()
    data["domain"] = domain

    assert Broker.model_validate(data).domain == domain


@pytest.mark.parametrize(
    "domain",
    ["", "example", "Example.org", "https://example.org", "example.org/rgpd", "-x.example.org"],
)
def test_invalid_domains_are_rejected(domain: str) -> None:
    data = valid_data()
    data["domain"] = domain

    with pytest.raises(pydantic.ValidationError):
        Broker.model_validate(data)


@pytest.mark.parametrize("language", ["", "FR", "fra", "f"])
def test_invalid_languages_are_rejected(language: str) -> None:
    data = valid_data()
    data["language"] = language

    with pytest.raises(pydantic.ValidationError):
        Broker.model_validate(data)


@pytest.mark.parametrize("name", ["", "   "])
def test_blank_name_is_rejected(name: str) -> None:
    data = valid_data()
    data["name"] = name

    with pytest.raises(pydantic.ValidationError):
        Broker.model_validate(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("region", "de"),
        ("category", "inconnue"),
        ("legal_basis", ["gdpr_art99"]),
        ("required_fields", ["social_security_number"]),
        ("source", "elsewhere"),
    ],
)
def test_values_outside_closed_lists_are_rejected(field: str, value: object) -> None:
    data = valid_data()
    data[field] = value

    with pytest.raises(pydantic.ValidationError):
        Broker.model_validate(data)


@pytest.mark.parametrize("field", ["legal_basis", "required_fields"])
def test_lists_must_not_be_empty(field: str) -> None:
    data = valid_data()
    data[field] = []

    with pytest.raises(pydantic.ValidationError):
        Broker.model_validate(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [("legal_basis", ["gdpr_art17", "gdpr_art17"]), ("required_fields", ["email", "email"])],
)
def test_lists_must_not_contain_duplicates(field: str, value: list[str]) -> None:
    data = valid_data()
    data[field] = value

    with pytest.raises(pydantic.ValidationError):
        Broker.model_validate(data)


@pytest.mark.parametrize("requires_id", ["yes", "false", 0, 1])
def test_requires_id_must_be_a_real_boolean(requires_id: object) -> None:
    data = valid_data()
    data["requires_id"] = requires_id

    with pytest.raises(pydantic.ValidationError):
        Broker.model_validate(data)


@pytest.mark.parametrize("address", ["", "dpo", "dpo@", "@example.org", "dpo@example", "a b@x.fr"])
def test_invalid_email_addresses_are_rejected(address: str) -> None:
    data = valid_data()
    data["channels"] = [{"type": "email", "address": address}]

    with pytest.raises(pydantic.ValidationError):
        Broker.model_validate(data)


@pytest.mark.parametrize("url", ["", "example.org/rgpd", "ftp://example.org/rgpd"])
def test_invalid_web_form_urls_are_rejected(url: str) -> None:
    data = valid_data()
    data["channels"] = [{"type": "web_form", "url": url, "steps": []}]

    with pytest.raises(pydantic.ValidationError):
        Broker.model_validate(data)


def test_blank_postal_address_is_rejected() -> None:
    data = valid_data()
    data["channels"] = [{"type": "postal", "address": "  "}]

    with pytest.raises(pydantic.ValidationError):
        Broker.model_validate(data)


# --- Provenance: source_ref and last_verified -----------------------------------


def test_last_verified_accepts_iso_strings() -> None:
    data = valid_data()
    data["last_verified"] = "2026-10-24"

    assert Broker.model_validate(data).last_verified == date(2026, 10, 24)


@pytest.mark.parametrize("value", ["24/10/2026", "2026-13-01", "hier"])
def test_last_verified_must_be_a_date(value: str) -> None:
    data = valid_data()
    data["last_verified"] = value

    with pytest.raises(pydantic.ValidationError):
        Broker.model_validate(data)


@pytest.mark.parametrize("missing", ["absent", "null"])
def test_manual_entries_require_last_verified(missing: str) -> None:
    data = valid_data()
    if missing == "absent":
        del data["last_verified"]
    else:
        data["last_verified"] = None

    with pytest.raises(pydantic.ValidationError) as exc_info:
        Broker.model_validate(data)

    assert ("last_verified",) in error_fields(exc_info.value)


@pytest.mark.parametrize("source", ["eraser", "optout", "badbool", "optery"])
def test_imported_entries_may_omit_last_verified(source: str) -> None:
    data = valid_data()
    data["source"] = source
    del data["last_verified"]

    assert Broker.model_validate(data).last_verified is None


@pytest.mark.parametrize("source_ref", ["", "   "])
def test_source_ref_must_not_be_blank(source_ref: str) -> None:
    data = valid_data()
    data["source_ref"] = source_ref

    with pytest.raises(pydantic.ValidationError):
        Broker.model_validate(data)


# --- Country --------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["absent", "null"])
def test_manual_entries_require_a_country(missing: str) -> None:
    data = valid_data()
    if missing == "absent":
        del data["country"]
    else:
        data["country"] = None

    with pytest.raises(pydantic.ValidationError, match="country"):
        Broker.model_validate(data)


@pytest.mark.parametrize("source", ["eraser", "optout", "badbool", "optery"])
def test_imported_entries_may_omit_the_country(source: str) -> None:
    """Upstream lists rarely give the country, and their region is not one."""
    data = valid_data()
    data["source"] = source
    del data["country"]

    assert Broker.model_validate(data).country is None


def test_fields_why_only_for_sent_fields(fixtures_brokers_dir: Path) -> None:
    path = fixtures_brokers_dir / "fr" / "courtier-fictif.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["fields_why"] = {"postal_address": "Estimation : annuaire classé par adresse."}
    assert Broker.model_validate(data).fields_why

    data["fields_why"] = {"phone": "Estimation : sans objet."}
    with pytest.raises(pydantic.ValidationError, match="phone"):
        Broker.model_validate(data)
