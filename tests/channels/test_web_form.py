"""Tests for the web form engine, against a fake page (no browser, no network)."""

from pathlib import Path
from typing import Any

import pytest
from fakes import FakePage, FakePrompter

from gomme.brokers.schema import Broker, WebFormChannel
from gomme.channels.web_form import StepFailedError, run_steps, verify_form

FORM = "https://www.example.org/optout"


def channel(steps: list[dict[str, Any]], required: list[str] | None = None) -> WebFormChannel:
    broker = Broker.model_validate(
        {
            "slug": "fake-form",
            "name": "Fake Form (TEST FIXTURE)",
            "domain": "example.org",
            "region": "us",
            "category": "people-search",
            "channels": [{"type": "web_form", "url": FORM, "steps": steps}],
            "legal_basis": ["ccpa_1798_105"],
            "required_fields": required or ["full_name", "email", "postal_address"],
            "requires_id": False,
            "language": "en",
            "source": "eraser",
            "source_ref": "https://example.org/fixture",
        }
    )
    form = broker.channels[0]
    assert isinstance(form, WebFormChannel)
    return form


VALUES = {
    "first_name": "Jane",
    "last_name": "Doe",
    "full_name": "Jane Doe",
    "name_slug": "jane-doe",
    "name_title_slug": "Jane-Doe",
    "email": "jane@example.org",
    "state": "NY",
    "state_slug": "New-York",
    "city": "Nowhere",
    "street": "1 Example St",
    "postal_code": "00000",
    "state_name": "New York",
}


def run(form: WebFormChannel, page: FakePage, prompter: FakePrompter, tmp_path: Path) -> Any:
    return run_steps(
        form, page, prompter, VALUES, language="fr", proof_path=tmp_path / "1-fake-form.png"
    )


def test_full_flow_with_search_and_capture(tmp_path: Path) -> None:
    form = channel(
        [
            {
                "id": "find",
                "type": "search",
                "url": "https://www.example.org/{name_title_slug}/{state_slug}",
                "result_selector": ".results",
            },
            {"id": "open", "type": "navigate"},
            {
                "id": "fill",
                "type": "form_fill",
                "fields": {
                    "url": {"selector": "#url", "value": "{listing_url}", "input": "js"},
                    "email": {"selector": "#email", "value": "{email}"},
                },
            },
            {"id": "captcha", "type": "prompt_user", "message": {"fr": "Résolvez le CAPTCHA."}},
            {"id": "submit", "type": "click", "selector": "li:has-text('{state}')"},
            {"id": "proof", "type": "capture"},
        ]
    )
    page = FakePage({".results"}, url_after_pause="https://www.example.org/Jane-Doe/NY/123")
    prompter = FakePrompter(page)

    result = run(form, page, prompter, tmp_path)

    assert page.log == [
        ("goto", "https://www.example.org/Jane-Doe/New-York"),
        ("wait", ".results"),
        ("goto", FORM),
        ("fill", "#url", "https://www.example.org/Jane-Doe/NY/123", "js", "None"),
        ("fill", "#email", "jane@example.org", "fill", "None"),
        ("click", "li:has-text('NY')"),
        ("screenshot", "1-fake-form-1.png"),
    ]
    assert prompter.pauses[1] == "Résolvez le CAPTCHA."
    assert result.listing_url == "https://www.example.org/Jane-Doe/NY/123"
    assert result.screenshots == [tmp_path / "1-fake-form-1.png"]


def test_listing_url_is_asked_when_the_page_did_not_change(tmp_path: Path) -> None:
    form = channel(
        [
            {
                "id": "find",
                "type": "search",
                "url": "https://www.example.org/s",
                "result_selector": ".r",
            }
        ]
    )
    page = FakePage({".r"})

    result = run(form, page, FakePrompter(page, ["https://www.example.org/me"]), tmp_path)

    assert result.listing_url == "https://www.example.org/me"


def test_search_without_a_listing_fails(tmp_path: Path) -> None:
    form = channel(
        [
            {
                "id": "find",
                "type": "search",
                "url": "https://www.example.org/s",
                "result_selector": ".r",
            }
        ]
    )
    page = FakePage({".r"})

    with pytest.raises(StepFailedError, match="aucune fiche"):
        run(form, page, FakePrompter(page, [""]), tmp_path)


def test_prompt_if_present_only_pauses_when_the_element_is_there(tmp_path: Path) -> None:
    steps = [
        {
            "id": "a",
            "type": "prompt_user_if_present",
            "selector": ".captcha",
            "message": {"en": "A"},
        },
        {"id": "b", "type": "prompt_user_if_present", "selector": ".other", "message": {"en": "B"}},
    ]
    page = FakePage({".captcha"})
    prompter = FakePrompter(page)

    run(channel(steps), page, prompter, tmp_path)

    assert prompter.pauses == ["A"]  # French missing: falls back to English


def test_failed_step_stops_the_run(tmp_path: Path) -> None:
    steps = [
        {"id": "wait", "type": "wait_for", "selector": "#form", "timeout_ms": 1000},
        {"id": "proof", "type": "capture"},
    ]
    page = FakePage()

    with pytest.raises(StepFailedError, match="étape wait : TimeoutError"):
        run(channel(steps), page, FakePrompter(page), tmp_path)
    assert ("screenshot", "1-fake-form-1.png") not in page.log


def test_failed_optional_step_is_a_warning(tmp_path: Path) -> None:
    steps = [
        {"id": "check", "type": "assert", "selector": ".ok", "optional": True},
        {"id": "proof", "type": "capture"},
    ]
    page = FakePage()

    result = run(channel(steps), page, FakePrompter(page), tmp_path)

    assert result.warnings == ["étape check : élément attendu absent"]
    assert result.screenshots


def test_browser_errors_do_not_echo_typed_values(tmp_path: Path) -> None:
    steps = [
        {"id": "fill", "type": "form_fill", "fields": {"e": {"selector": "#e", "value": "{email}"}}}
    ]
    page = FakePage()
    page.failing.add("#e")

    with pytest.raises(StepFailedError) as exc_info:
        run(channel(steps), page, FakePrompter(page), tmp_path)

    assert "jane@example.org" not in str(exc_info.value)


def test_assert_conditions(tmp_path: Path) -> None:
    steps = [
        {"id": "a", "type": "assert", "text_contains": "enregistrée"},
        {"id": "b", "type": "assert", "url_matches": r"/optout$"},
        {"id": "c", "type": "assert", "js_condition": "() => true"},
    ]
    page = FakePage()
    page.current_url = FORM

    assert run(channel(steps), page, FakePrompter(page), tmp_path).warnings == []

    page.current_url = "https://www.example.org/error"
    with pytest.raises(StepFailedError, match="adresse de page inattendue"):
        run(channel(steps), page, FakePrompter(page), tmp_path)


def test_wait_message_is_shown(tmp_path: Path) -> None:
    steps = [
        {
            "id": "turnstile",
            "type": "wait_for",
            "js_condition": "() => true",
            "message": {"fr": "Contrôle de sécurité…", "en": "Security check…"},
        }
    ]
    page = FakePage()
    prompter = FakePrompter(page)

    run(channel(steps), page, prompter, tmp_path)

    assert "Contrôle de sécurité…" in prompter.shown
    assert ("wait_js", "() => true") in page.log


# --- verify ------------------------------------------------------------------------------------


def test_verify_checks_elements_until_the_first_interaction() -> None:
    steps = [
        {"id": "open", "type": "navigate"},
        {"id": "wait", "type": "wait_for", "selector": "#form"},
        {
            "id": "fill",
            "type": "form_fill",
            "fields": {
                "name": {"selector": "#name", "value": "{full_name}"},
                "email": {"selector": "#gone", "value": "{email}"},
            },
        },
        {"id": "submit", "type": "click", "selector": "#submit"},
        {"id": "after", "type": "wait_for", "selector": "#never-checked"},
    ]
    page = FakePage({"#form", "#name"})

    result = verify_form(channel(steps), page)

    assert result.checked == ["wait", "fill.name"]
    assert result.missing == ["fill.email"]
    assert result.stopped_at == "submit"
    assert not any(entry[0] == "fill" for entry in page.log)  # nothing typed


def test_verify_never_opens_a_url_with_personal_data() -> None:
    steps = [
        {
            "id": "s",
            "type": "search",
            "url": "https://www.example.org/{name_slug}",
            "result_selector": ".r",
        }
    ]
    page = FakePage()

    result = verify_form(channel(steps), page)

    assert page.log == []
    assert result.stopped_at == "s"
