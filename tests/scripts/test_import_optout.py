"""Tests for scripts/import_optout.py, on fictional upstream data only."""

from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from _import_common import UpstreamError
from import_optout import OPTOUT_BROKERS_PATH, convert_template, import_optout, main

from gomme.brokers.registry import load_registry
from gomme.brokers.schema import (
    Broker,
    EmailChannel,
    FormFillStep,
    InputMethod,
    SearchStep,
    WebFormChannel,
)

from .conftest import MakeRepo


def optout_broker(**overrides: Any) -> dict[str, Any]:
    """A fictional definition in OptOut's format."""
    data: dict[str, Any] = {
        "slug": "fake-search",
        "name": "Fake Search (TEST FIXTURE)",
        "domain": "example.org",
        "category": "people_search",
        "parent_company": None,
        "method": "web_form",
        "opt_out_url": "https://www.example.org/optout",
        "opt_out_email": "privacy@example.org",
        "opt_out_postal_address": None,
        "required_fields": ["listing_url", "full_name"],
        "optional_fields": ["dob"],
        "requires_listing_url": True,
        "requires_phone_verification": False,
        "requires_email_verification": True,
        "requires_id_upload": False,
        "requires_notarization": False,
        "legal_basis": ["CCPA", "CPRA"],
        "statutory_response_days": 45,
        "re_add_window_days": 60,
        "last_verified_at": date(2026, 5, 10),
        "notes": "FAKE: test fixture.",
        "steps": [
            {
                "id": "find",
                "type": "search",
                "label": "Find your listing",
                "search_url_template": "https://www.example.org/{name_title_slug}/{state_full}",
                "result_selector": ".result",
                "pick_strategy": "prompt_user",
            },
            {"id": "open", "type": "navigate", "url": "https://www.example.org/optout"},
            {
                "id": "fill",
                "type": "form_fill",
                "fields": {
                    "url": {
                        "selector": "#url",
                        "value": "{state.found_listing_url}",
                        "use_js": True,
                    },
                    "age": {"selector": "#age", "value": "{age}", "use_type": True},
                },
            },
            {
                "id": "verify",
                "type": "prompt_user",
                "message": "Check {profile.emails.current[0]}.",
            },
            {"id": "check", "type": "assert", "selector": ".ok", "optional": True},
            {
                "id": "proof",
                "type": "capture",
                "method": "screenshot",
                "save_as": "{submission.id}.png",
            },
        ],
    }
    data.update(overrides)
    return data


@pytest.fixture
def translations(tmp_path: Path) -> Path:
    path = tmp_path / "optout_fr.yml"
    texts = {
        "fake-search": {
            "find": {"label": "Trouver votre fiche"},
            "verify": {"message": "Vérifiez {email}."},
        }
    }
    path.write_text(yaml.safe_dump(texts, allow_unicode=True), encoding="utf-8")
    return path


def optout_repo(make_repo: MakeRepo, *brokers: dict[str, Any]) -> Path:
    return make_repo(
        "optout", {f"{OPTOUT_BROKERS_PATH}/{broker['slug']}.yml": broker for broker in brokers}
    )


def only_broker(brokers_dir: Path) -> Broker:
    (broker,) = load_registry(brokers_dir)
    return broker


# --- Conversion ---------------------------------------------------------------------


def test_imported_definition_is_valid_and_complete(
    make_repo: MakeRepo, brokers_dir: Path, translations: Path
) -> None:
    repo = optout_repo(make_repo, optout_broker())

    report = import_optout(repo, brokers_dir, translations_path=translations)

    assert report.written == ["fake-search"]
    broker = only_broker(brokers_dir)
    assert broker.region == "us"
    assert broker.category == "people-search"
    assert broker.country is None
    assert broker.legal_basis == ("ccpa_1798_105",)
    assert broker.last_verified == date(2026, 5, 10)
    assert broker.source == "optout"
    assert broker.source_ref.endswith(f"{OPTOUT_BROKERS_PATH}/fake-search.yml")
    assert "/blob/" in broker.source_ref
    assert "Confirmation requise" in broker.notes
    assert "FAKE: test fixture." in broker.notes
    form, email = broker.channels
    assert isinstance(form, WebFormChannel)
    assert email == EmailChannel(type="email", address="privacy@example.org")
    assert [step.type for step in form.steps] == [
        "search",
        "navigate",
        "form_fill",
        "prompt_user",
        "assert",
        "capture",
    ]


def test_placeholders_and_options_are_converted(
    make_repo: MakeRepo, brokers_dir: Path, translations: Path
) -> None:
    import_optout(
        optout_repo(make_repo, optout_broker()), brokers_dir, translations_path=translations
    )

    form = only_broker(brokers_dir).channels[0]
    assert isinstance(form, WebFormChannel)
    search, _, fill, verify, check, _ = form.steps
    assert isinstance(search, SearchStep)
    assert search.url == "https://www.example.org/{name_title_slug}/{state_slug}"
    assert isinstance(fill, FormFillStep)
    assert fill.fields["url"].value == "{listing_url}"
    assert fill.fields["url"].input is InputMethod.JS
    assert fill.fields["age"].input is InputMethod.TYPE
    assert verify.type == "prompt_user"
    assert verify.message.en == "Check {email}."
    assert check.optional is True


def test_french_texts_come_from_the_translations(
    make_repo: MakeRepo, brokers_dir: Path, translations: Path
) -> None:
    report = import_optout(
        optout_repo(make_repo, optout_broker()), brokers_dir, translations_path=translations
    )

    form = only_broker(brokers_dir).channels[0]
    assert isinstance(form, WebFormChannel)
    label = form.steps[0].label
    assert label is not None
    assert (label.fr, label.en) == ("Trouver votre fiche", "Find your listing")
    assert report.warnings == []


def test_steps_add_the_fields_optout_does_not_declare(
    make_repo: MakeRepo, brokers_dir: Path, translations: Path
) -> None:
    """The form sends the age and the search URL the state: both must be declared."""
    import_optout(
        optout_repo(make_repo, optout_broker()), brokers_dir, translations_path=translations
    )

    assert only_broker(brokers_dir).required_fields == (
        "full_name",
        "email",
        "postal_address",
        "birth_date",
    )


def test_missing_translations_are_reported(
    make_repo: MakeRepo, brokers_dir: Path, tmp_path: Path
) -> None:
    empty = tmp_path / "empty.yml"
    empty.write_text("{}\n", encoding="utf-8")

    report = import_optout(
        optout_repo(make_repo, optout_broker()), brokers_dir, translations_path=empty
    )

    assert report.warnings == [
        "texte sans traduction française : fake-search.find.label",
        "texte sans traduction française : fake-search.verify.message",
    ]


@pytest.mark.parametrize(
    ("texts", "message"),
    [
        ({"fake-search": {"gone": {"label": "x"}}}, "sans étape correspondante"),
        ({"other-broker": {"find": {"label": "x"}}}, "fiches absentes"),
    ],
)
def test_stale_translations_are_rejected(
    make_repo: MakeRepo, brokers_dir: Path, tmp_path: Path, texts: object, message: str
) -> None:
    path = tmp_path / "stale.yml"
    path.write_text(yaml.safe_dump(texts), encoding="utf-8")

    with pytest.raises(UpstreamError, match=message):
        import_optout(optout_repo(make_repo, optout_broker()), brokers_dir, translations_path=path)


def test_convert_template() -> None:
    assert convert_template("{name_first} {name_last}") == "{first_name} {last_name}"
    assert convert_template("{profile.current_address.zip}") == "{postal_code}"
    with pytest.raises(UpstreamError, match="non prise en charge"):
        convert_template("{profile.dob}")


@pytest.mark.parametrize(
    "step",
    [
        {"id": "sms", "type": "sms_verification_prompt", "code_selector": "#c", "message": "m"},
        {"id": "open", "type": "navigate", "url": "https://www.example.org", "extra": 1},
        {"id": "open", "type": "navigate", "url": "https://www.example.org", "on_failure": "skip"},
        {"id": "fill", "type": "form_fill", "fields": {"a": {"selector": "#a", "value": "{x}"}}},
    ],
)
def test_unsupported_upstream_steps_are_rejected(
    make_repo: MakeRepo, brokers_dir: Path, translations: Path, step: dict[str, Any]
) -> None:
    repo = optout_repo(make_repo, optout_broker(steps=[step]))

    with pytest.raises(UpstreamError, match=r"fake-search\.yml"):
        import_optout(repo, brokers_dir, translations_path=translations)


# --- Ownership and priorities -----------------------------------------------------------


def test_rerun_leaves_files_unchanged(
    make_repo: MakeRepo, brokers_dir: Path, translations: Path
) -> None:
    repo = optout_repo(make_repo, optout_broker())
    import_optout(repo, brokers_dir, translations_path=translations)

    report = import_optout(repo, brokers_dir, translations_path=translations)

    assert (report.written, report.unchanged) == ([], ["fake-search"])


def test_broker_dropped_upstream_is_removed(
    make_repo: MakeRepo, brokers_dir: Path, translations: Path
) -> None:
    other = optout_broker(slug="other-search", domain="example.net", steps=[], method="email")
    repo = optout_repo(make_repo, optout_broker(), other)
    import_optout(repo, brokers_dir, translations_path=translations)
    (repo / OPTOUT_BROKERS_PATH / "other-search.yml").unlink()
    make_repo("optout", {})

    report = import_optout(repo, brokers_dir, translations_path=translations)

    assert report.removed == ["other-search"]
    assert [broker.slug for broker in load_registry(brokers_dir)] == ["fake-search"]


def test_manual_definition_on_the_same_domain_wins(
    make_repo: MakeRepo, brokers_dir: Path, translations: Path
) -> None:
    manual = brokers_dir / "us" / "fake-manual.yml"
    manual.parent.mkdir()
    manual.write_text(
        yaml.safe_dump(
            {
                "slug": "fake-manual",
                "name": "Fake Manual (TEST FIXTURE)",
                "domain": "example.org",
                "country": "US",
                "region": "us",
                "category": "people-search",
                "channels": [{"type": "email", "address": "dpo@example.org"}],
                "legal_basis": ["ccpa_1798_105"],
                "required_fields": ["full_name", "email"],
                "requires_id": False,
                "language": "en",
                "source": "manual",
                "source_ref": "https://example.org/fixture",
                "last_verified": "2026-10-25",
            }
        ),
        encoding="utf-8",
    )
    before = manual.read_text(encoding="utf-8")

    report = import_optout(
        optout_repo(make_repo, optout_broker()), brokers_dir, translations_path=translations
    )

    assert report.skipped == [("fake-search", "domaine déjà couvert par fake-manual (manual)")]
    assert [broker.slug for broker in load_registry(brokers_dir)] == ["fake-manual"]
    assert manual.read_text(encoding="utf-8") == before


def test_eraser_email_fills_in_a_missing_address(
    make_repo: MakeRepo, brokers_dir: Path, translations: Path
) -> None:
    eraser = make_repo(
        "eraser",
        {
            "data/brokers.yaml": {
                "brokers": [
                    {
                        "id": "fake-search",
                        "name": "Fake Search",
                        "email": "optout@example.org",
                        "website": "https://www.example.org",
                        "region": "us",
                        "category": "people-search",
                        "notes": "FAKE: Eraser fixture.",
                    }
                ]
            }
        },
    )
    repo = optout_repo(make_repo, optout_broker(opt_out_email=None))

    import_optout(repo, brokers_dir, eraser_repo=eraser, translations_path=translations)

    broker = only_broker(brokers_dir)
    assert broker.channels[1] == EmailChannel(type="email", address="optout@example.org")
    assert "adresse email reprise d'Eraser" in broker.notes
    assert "data/brokers.yaml#L2" in broker.notes
    assert "FAKE: Eraser fixture." in broker.notes


def test_dirty_checkout_is_refused(
    make_repo: MakeRepo, brokers_dir: Path, translations: Path
) -> None:
    repo = optout_repo(make_repo, optout_broker())
    (repo / "uncommitted.txt").write_text("x", encoding="utf-8")

    with pytest.raises(UpstreamError, match="modifications locales"):
        import_optout(repo, brokers_dir, translations_path=translations)


def test_main_reports_errors_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--repo", str(tmp_path / "missing"), "--brokers-dir", str(tmp_path)])

    assert code == 1
    assert "Import impossible" in capsys.readouterr().err
