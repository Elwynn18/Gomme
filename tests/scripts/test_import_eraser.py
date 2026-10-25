"""Tests for scripts/import_eraser.py, on fictional upstream data only."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from _import_common import UpstreamError
from import_eraser import OUT_OF_SCOPE, import_eraser

from gomme.brokers.registry import load_registry
from gomme.brokers.schema import Broker, EmailChannel, WebFormChannel

from .conftest import MakeRepo


def entry(entry_id: str, **overrides: Any) -> dict[str, Any]:
    """A fictional entry in Eraser's format (.example is a reserved TLD)."""
    data: dict[str, Any] = {
        "id": entry_id,
        "name": f"{entry_id} (TEST FIXTURE)",
        "email": f"privacy@{entry_id}.example",
        "website": f"https://www.{entry_id}.example",
        "region": "us",
        "category": "marketing",
    }
    data.update(overrides)
    return data


def eraser_repo(make_repo: MakeRepo, *entries: dict[str, Any]) -> Path:
    return make_repo("eraser", {"data/brokers.yaml": {"brokers": list(entries)}})


def imported(brokers_dir: Path) -> dict[str, Broker]:
    return {broker.slug: broker for broker in load_registry(brokers_dir)}


def test_marketing_broker_with_email_and_page(make_repo: MakeRepo, brokers_dir: Path) -> None:
    repo = eraser_repo(
        make_repo, entry("broker-a", opt_out_url="https://privacy.elsewhere.example/form")
    )

    report = import_eraser(repo, brokers_dir)

    assert report.written == ["broker-a"]
    broker = imported(brokers_dir)["broker-a"]
    assert broker.domain == "broker-a.example"
    assert broker.country is None
    assert broker.region == "us"
    assert broker.legal_basis == ("ccpa_1798_105",)
    assert broker.required_fields == ("full_name", "email")
    assert broker.requires_id is False
    assert broker.last_verified is None
    assert broker.source == "eraser"
    assert broker.source_ref.endswith("/data/brokers.yaml#L2")
    email, form = broker.channels
    assert email == EmailChannel(type="email", address="privacy@broker-a.example")
    assert isinstance(form, WebFormChannel)
    assert form.steps == ()  # a page the user fills in by hand


def test_people_search_also_needs_the_address(make_repo: MakeRepo, brokers_dir: Path) -> None:
    import_eraser(eraser_repo(make_repo, entry("broker-a", category="people-search")), brokers_dir)

    assert imported(brokers_dir)["broker-a"].required_fields == (
        "full_name",
        "email",
        "postal_address",
    )


def test_global_brokers_can_be_asked_under_gdpr_and_ccpa(
    make_repo: MakeRepo, brokers_dir: Path
) -> None:
    import_eraser(eraser_repo(make_repo, entry("broker-a", region="global")), brokers_dir)

    broker = imported(brokers_dir)["broker-a"]
    assert broker.region == "global"
    assert broker.legal_basis == ("gdpr_art17", "gdpr_art21", "ccpa_1798_105")
    assert (brokers_dir / "global" / "broker-a.yml").is_file()


def test_id_requirement_is_flagged(make_repo: MakeRepo, brokers_dir: Path) -> None:
    repo = eraser_repo(make_repo, entry("broker-a", category="requires-id", notes="Asked for ID."))

    import_eraser(repo, brokers_dir)

    broker = imported(brokers_dir)["broker-a"]
    assert broker.requires_id is True
    assert broker.category == "autre"
    assert "caviardée" in broker.notes
    assert "Notes d'Eraser : Asked for ID." in broker.notes


def test_web_form_only_broker(make_repo: MakeRepo, brokers_dir: Path) -> None:
    repo = eraser_repo(
        make_repo, entry("broker-a", email="", opt_out_url="https://www.broker-a.example/optout")
    )

    import_eraser(repo, brokers_dir)

    (channel,) = imported(brokers_dir)["broker-a"].channels
    assert isinstance(channel, WebFormChannel)


def test_unusable_and_out_of_scope_entries_are_skipped(
    make_repo: MakeRepo, brokers_dir: Path
) -> None:
    repo = eraser_repo(
        make_repo,
        entry("broker-eu", region="eu"),
        entry("broker-ads", category="device-id-only"),
        entry("nosis", region="global"),
        entry("broker-silent", email="", opt_out_url=""),
        entry("broker-typo", email="privacy@broker-typo"),
        entry("broker-ok"),
    )

    report = import_eraser(repo, brokers_dir)

    assert report.written == ["broker-ok"]
    reasons = dict(report.skipped)
    assert "broker-eu" not in reasons  # European entries are written by hand, not imported
    assert reasons["broker-ads"] == "n'accepte qu'un identifiant publicitaire"
    assert reasons["nosis"] == "hors périmètre : bureau de crédit argentin"
    assert reasons["broker-silent"] == "aucun canal de contact"
    assert reasons["broker-typo"].startswith("invalide (channels.0.email.address")


def test_reviewed_exclusions_that_vanished_upstream_are_reported(
    make_repo: MakeRepo, brokers_dir: Path
) -> None:
    report = import_eraser(eraser_repo(make_repo, entry("broker-ok")), brokers_dir)

    assert len(report.warnings) == len(OUT_OF_SCOPE)


def test_unknown_category_stops_the_import(make_repo: MakeRepo, brokers_dir: Path) -> None:
    repo = eraser_repo(make_repo, entry("broker-a", category="crypto"))

    with pytest.raises(UpstreamError, match="catégorie Eraser inconnue"):
        import_eraser(repo, brokers_dir)


def test_unknown_upstream_keys_stop_the_import(make_repo: MakeRepo, brokers_dir: Path) -> None:
    repo = eraser_repo(make_repo, entry("broker-a", phone="000"))

    with pytest.raises(UpstreamError, match=r"brokers\.yaml:2"):
        import_eraser(repo, brokers_dir)


def test_broker_covered_by_optout_is_skipped(make_repo: MakeRepo, brokers_dir: Path) -> None:
    optout_definition = {
        "slug": "broker-a-search",
        "name": "Broker A (TEST FIXTURE)",
        "domain": "broker-a.example",
        "region": "us",
        "category": "people-search",
        "channels": [{"type": "email", "address": "privacy@broker-a.example"}],
        "legal_basis": ["ccpa_1798_105"],
        "required_fields": ["full_name", "email"],
        "requires_id": False,
        "language": "en",
        "source": "optout",
        "source_ref": "https://example.org/fixture",
    }
    path = brokers_dir / "us" / "broker-a-search.yml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(optout_definition), encoding="utf-8")

    report = import_eraser(eraser_repo(make_repo, entry("broker-a")), brokers_dir)

    assert report.skipped == [("broker-a", "domaine déjà couvert par broker-a-search (optout)")]
    assert list(imported(brokers_dir)) == ["broker-a-search"]


def test_entries_dropped_or_moved_upstream_are_cleaned_up(
    make_repo: MakeRepo, brokers_dir: Path
) -> None:
    import_eraser(eraser_repo(make_repo, entry("broker-a"), entry("broker-b")), brokers_dir)

    report = import_eraser(eraser_repo(make_repo, entry("broker-a", region="global")), brokers_dir)

    assert report.removed == ["broker-b"]
    assert {slug: broker.region for slug, broker in imported(brokers_dir).items()} == {
        "broker-a": "global"
    }
    assert not (brokers_dir / "us" / "broker-a.yml").exists()


def test_generated_files_say_where_they_come_from(make_repo: MakeRepo, brokers_dir: Path) -> None:
    import_eraser(eraser_repo(make_repo, entry("broker-a")), brokers_dir)

    text = (brokers_dir / "us" / "broker-a.yml").read_text(encoding="utf-8")
    assert text.startswith("# Généré par scripts/import_eraser.py")
    assert "LICENSES/eraser-MIT.txt" in text
    assert "notes:" not in text  # empty optional keys are left out
