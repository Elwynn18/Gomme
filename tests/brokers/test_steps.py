"""Tests for web form steps: strict parameters, placeholders and safety rules."""

from typing import Any

import pydantic
import pytest

from gomme.brokers.schema import (
    Broker,
    FormFillStep,
    InputMethod,
    LocalizedText,
    NavigateStep,
    StepType,
    WebFormChannel,
)

FORM_URL = "https://www.example.org/opt-out"

ONE_STEP_OF_EACH_TYPE: list[dict[str, Any]] = [
    {"id": "open", "type": "navigate"},
    {
        "id": "find_listing",
        "type": "search",
        "url": "https://www.example.org/search/{name_slug}",
        "result_selector": ".result",
    },
    {
        "id": "fill",
        "type": "form_fill",
        "fields": {"listing": {"selector": "#url", "value": "{listing_url}"}},
    },
    {"id": "next", "type": "click", "selector": "#next"},
    {"id": "wait", "type": "wait_for", "selector": "#done"},
    {"id": "confirm", "type": "prompt_user", "message": {"fr": "Cliquez sur le lien reçu."}},
    {
        "id": "captcha",
        "type": "prompt_user_if_present",
        "selector": ".captcha",
        "message": {"en": "Solve the CAPTCHA."},
    },
    {"id": "check", "type": "assert", "url_matches": r"/done$", "optional": True},
    {"id": "proof", "type": "capture"},
]


def broker_with_steps(
    steps: list[dict[str, Any]],
    required_fields: list[str] | None = None,
    url: str = FORM_URL,
) -> dict[str, Any]:
    """A valid, entirely fictional broker whose first channel is an automated form."""
    return {
        "slug": "fake-form",
        "name": "Fake Form (TEST FIXTURE)",
        "domain": "example.org",
        "country": "US",
        "region": "us",
        "category": "people-search",
        "channels": [{"type": "web_form", "url": url, "steps": steps}],
        "legal_basis": ["ccpa_1798_105"],
        "required_fields": required_fields or ["full_name", "email"],
        "requires_id": False,
        "language": "en",
        "source": "manual",
        "source_ref": "https://example.org/fixture",
        "last_verified": "2026-10-25",
    }


def web_form(data: dict[str, Any]) -> WebFormChannel:
    channel = Broker.model_validate(data).channels[0]
    assert isinstance(channel, WebFormChannel)
    return channel


def rejected(data: dict[str, Any]) -> str:
    with pytest.raises(pydantic.ValidationError) as exc_info:
        Broker.model_validate(data)
    return str(exc_info.value)


# --- Step types -------------------------------------------------------------------


def test_every_step_type_is_supported() -> None:
    channel = web_form(broker_with_steps(ONE_STEP_OF_EACH_TYPE))

    assert [step.type for step in channel.steps] == [step.value for step in StepType]


def test_web_form_steps_default_to_empty() -> None:
    data = broker_with_steps([])
    del data["channels"][0]["steps"]

    assert web_form(data).steps == ()


def test_navigate_url_defaults_to_the_channel_url() -> None:
    (step,) = web_form(broker_with_steps([{"id": "open", "type": "navigate"}])).steps

    assert isinstance(step, NavigateStep)
    assert step.url is None


def test_form_field_options() -> None:
    steps = [
        {
            "id": "fill",
            "type": "form_fill",
            "frame": "iframe[src*='forms']",
            "fields": {
                "email": {"selector": "#email", "value": "{email}", "input": "js", "force": True}
            },
        }
    ]

    (step,) = web_form(broker_with_steps(steps)).steps

    assert isinstance(step, FormFillStep)
    assert step.fields["email"].input is InputMethod.JS
    assert step.fields["email"].force is True


@pytest.mark.parametrize(
    "step",
    [
        {"id": "x", "type": "solve_captcha"},  # never: the user solves CAPTCHAs
        {"type": "capture"},  # id is required
        {"id": "Bad-Id", "type": "capture"},
        {"id": "x", "type": "capture", "save_as": "proof.png"},  # unknown parameter
        {"id": "x", "type": "click"},  # selector is required
        {"id": "x", "type": "prompt_user"},  # message is required
        {"id": "x", "type": "prompt_user", "message": {}},
        {"id": "x", "type": "prompt_user", "message": {"de": "Hallo"}},
        {"id": "x", "type": "prompt_user", "message": "plain text"},
        {"id": "x", "type": "form_fill", "fields": {}},
        {
            "id": "x",
            "type": "form_fill",
            "fields": {"a": {"selector": "#a", "value": "v", "input": "paste"}},
        },
        {"id": "x", "type": "wait_for"},
        {"id": "x", "type": "wait_for", "selector": "#a", "js_condition": "() => true"},
        {"id": "x", "type": "wait_for", "selector": "#a", "timeout_ms": 0},
        {"id": "x", "type": "wait_for", "selector": "#a", "timeout_ms": "10"},
        {"id": "x", "type": "assert"},
        {"id": "x", "type": "assert", "url_matches": "(unclosed"},
        {"id": "x", "type": "search", "url": "https://www.example.org/s"},  # no result_selector
    ],
)
def test_invalid_steps_are_rejected(step: dict[str, Any]) -> None:
    rejected(broker_with_steps([step]))


def test_step_ids_must_be_unique_within_a_form() -> None:
    steps = [{"id": "proof", "type": "capture"}, {"id": "proof", "type": "capture"}]

    assert "en double" in rejected(broker_with_steps(steps))


# --- Placeholders -------------------------------------------------------------------


def test_unknown_placeholder_is_rejected() -> None:
    steps = [{"id": "open", "type": "navigate", "url": "https://www.example.org/{profile.ssn}"}]

    assert "variable inconnue" in rejected(broker_with_steps(steps))


def test_js_conditions_and_regexes_may_contain_braces() -> None:
    steps = [
        {"id": "wait", "type": "wait_for", "js_condition": "() => { return true; }"},
        {"id": "check", "type": "assert", "url_matches": r"/done/\d{3}$"},
    ]

    assert len(web_form(broker_with_steps(steps)).steps) == 2


def test_listing_url_requires_an_earlier_search() -> None:
    steps = [
        {
            "id": "fill",
            "type": "form_fill",
            "fields": {"listing": {"selector": "#url", "value": "{listing_url}"}},
        }
    ]

    assert "avant toute étape search" in rejected(broker_with_steps(steps))


def test_localized_text_falls_back_to_the_other_language() -> None:
    assert LocalizedText(fr="Bonjour").get("en") == "Bonjour"
    assert LocalizedText(en="Hello").get("fr") == "Hello"
    assert LocalizedText(fr="Bonjour", en="Hello").get("en") == "Hello"


# --- Minimisation: steps may only send declared fields ----------------------------


@pytest.mark.parametrize(
    ("placeholder", "field"),
    [
        ("{age}", "birth_date"),
        ("{phone}", "phone"),
        ("{postal_code}", "postal_address"),
        ("{state_name}", "postal_address"),
    ],
)
def test_steps_cannot_send_undeclared_fields(placeholder: str, field: str) -> None:
    steps = [
        {
            "id": "fill",
            "type": "form_fill",
            "fields": {"x": {"selector": "#x", "value": placeholder}},
        }
    ]

    message = rejected(broker_with_steps(steps, required_fields=["full_name", "email"]))

    assert "required_fields" in message
    assert field in message
    assert "fill" in message


def test_declared_fields_may_be_sent() -> None:
    steps = [
        {
            "id": "fill",
            "type": "form_fill",
            "fields": {"x": {"selector": "#x", "value": "{birth_year}"}},
        }
    ]

    web_form(broker_with_steps(steps, required_fields=["full_name", "email", "birth_date"]))


def test_placeholders_in_messages_and_selectors_count_too() -> None:
    steps = [
        {"id": "pick", "type": "click", "selector": "li:has-text('{state}')"},
        {"id": "call", "type": "prompt_user", "message": {"fr": "Décrochez au {phone}."}},
    ]

    message = rejected(broker_with_steps(steps, required_fields=["full_name"]))

    assert "required_fields" in message


# --- Automated forms stay on the broker's domain ---------------------------------


@pytest.mark.parametrize(
    "url", ["https://example.org/s/{name_slug}", "https://privacy.example.org/s/{name_slug}"]
)
def test_step_urls_on_the_broker_domain_are_accepted(url: str) -> None:
    steps = [{"id": "s", "type": "search", "url": url, "result_selector": ".r"}]

    web_form(broker_with_steps(steps))


@pytest.mark.parametrize(
    "url",
    [
        "https://elsewhere.test/s/{name_slug}",
        "https://example.org.elsewhere.test/s",
        "https://notexample.org/s",
    ],
)
def test_step_urls_off_the_broker_domain_are_rejected(url: str) -> None:
    steps = [{"id": "s", "type": "search", "url": url, "result_selector": ".r"}]

    assert "ne peut ouvrir que" in rejected(broker_with_steps(steps))


@pytest.mark.parametrize(
    "url", ["http://www.example.org/s", "www.example.org/s", "https://{city}.example.org/s"]
)
def test_step_urls_must_be_https_with_a_literal_host(url: str) -> None:
    rejected(broker_with_steps([{"id": "open", "type": "navigate", "url": url}]))


def test_automated_form_url_must_be_on_the_broker_domain() -> None:
    data = broker_with_steps(
        [{"id": "proof", "type": "capture"}], url="https://forms.elsewhere.test/x"
    )

    assert "ne peut ouvrir que" in rejected(data)


def test_manual_form_may_be_hosted_elsewhere() -> None:
    """Without steps the user fills the form themselves, often on a privacy platform."""
    data = broker_with_steps([], url="https://privacyportal.elsewhere.test/form")

    assert web_form(data).steps == ()
