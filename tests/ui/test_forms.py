"""Tests for the profile forms of the web interface (pure functions)."""

from datetime import date
from pathlib import Path

import pytest

from gomme.brokers.fields import ProfileField
from gomme.brokers.registry import load_registry
from gomme.config import Composer, Config, EmailConfig, EmailMode, SmtpConfig
from gomme.profile import Profile
from gomme.ui import forms

TODAY = date(2026, 10, 26)
VALID = {
    "first_name": "Jeanne",
    "last_name": "Exemple",
    "email": "jeanne.exemple@example.org",
    "country": "FR",
    "composer": "mail_app",
}


def values(**changes: str) -> dict[str, str]:
    return forms.clean({**VALID, **changes})


def test_clean_drops_unknown_fields_and_trims() -> None:
    cleaned = forms.clean({"first_name": "  Jeanne ", "csrf": "x", "other": "y"})

    assert cleaned["first_name"] == "Jeanne"
    assert "csrf" not in cleaned
    assert cleaned["city"] == ""


def test_valid_minimal_profile() -> None:
    assert forms.errors_for(values(), forms.FIELDS, TODAY) == {}


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"first_name": ""}, "first_name"),
        ({"last_name": "a\nb"}, "last_name"),
        ({"email": "jeanne@"}, "email"),
        ({"country": "ZZ"}, "country"),
        ({"birth_date": "2030-01-01"}, "birth_date"),
        ({"birth_date": "10/12/2002"}, "birth_date"),
        ({"phone": "appelez-moi"}, "phone"),
        ({"composer": "pigeon"}, "composer"),
    ],
)
def test_errors_point_at_the_field(changes: dict[str, str], field: str) -> None:
    errors = forms.errors_for(values(**changes), forms.FIELDS, TODAY)

    assert set(errors) == {field}


def test_errors_never_repeat_the_value() -> None:
    errors = forms.errors_for(values(email="secret-value@"), forms.FIELDS, TODAY)

    assert "secret-value" not in errors["email"]


def test_only_the_asked_fields_are_checked() -> None:
    assert forms.errors_for(values(email="bad"), ("first_name",), TODAY) == {}


def test_address_is_all_or_nothing() -> None:
    errors = forms.errors_for(values(street="1 rue X"), forms.FIELDS, TODAY)

    assert set(errors) == {"postal_code", "city"}


def test_us_address_needs_a_state() -> None:
    us = values(country="US", street="1 Main St", postal_code="10001", city="New York")

    assert set(forms.errors_for(us, forms.FIELDS, TODAY)) == {"state"}
    assert forms.errors_for({**us, "state": "NY"}, forms.FIELDS, TODAY) == {}


def test_build_config_from_scratch() -> None:
    config = forms.build_config(
        values(
            street="1 rue X",
            postal_code="00000",
            city="Nulle-Part",
            birth_date="2000-01-31",
            phone="06 00 00 00 00",
            composer="gmail",
        )
    )

    assert config.profile.postal_address is not None
    assert config.profile.postal_address.country == "FR"
    assert config.profile.birth_date == date(2000, 1, 31)
    assert config.email.composer is Composer.GMAIL
    assert config.email.mode is EmailMode.MANUAL


def test_build_config_keeps_the_rest_of_the_base(profile: Profile) -> None:
    base = Config(
        profile=profile,
        email=EmailConfig(
            mode=EmailMode.SMTP,
            smtp=SmtpConfig(host="smtp.example.org", username="u"),
            daily_limit=20,
        ),
    )

    config = forms.build_config(values(first_name="Jeanne-Marie"), base=base)

    assert config.profile.first_name == "Jeanne-Marie"
    assert config.profile.postal_address is None  # left empty in the form: removed
    assert config.email.smtp == base.email.smtp
    assert config.email.daily_limit == 20


def test_values_of_round_trip(profile: Profile) -> None:
    config = Config(profile=profile)

    assert forms.build_config(forms.clean(forms.values_of(config)), base=config) == config


def test_suggested_composer() -> None:
    assert forms.suggested_composer("x@gmail.com") is Composer.GMAIL
    assert forms.suggested_composer("x@GoogleMail.com") is Composer.GMAIL
    assert forms.suggested_composer("x@example.org") is Composer.MAIL_APP


def test_steps_navigation() -> None:
    identity = forms.step_named("identity")

    assert forms.next_step(identity, 1).name == "details"
    assert forms.next_step(identity, -1).name == "welcome"
    assert forms.next_step(forms.STEPS[0], -1) is forms.STEPS[0]
    assert forms.step_named("unknown") is forms.STEPS[0]
    every_field = [name for step in forms.STEPS for name in step.fields]
    assert sorted(every_field) == sorted(forms.FIELDS)


def test_fields_needed(fixtures_brokers_dir: Path) -> None:
    brokers = load_registry(fixtures_brokers_dir)

    needed = forms.fields_needed(brokers, "FR")

    assert needed[ProfileField.POSTAL_ADDRESS] == 1  # courtier-fictif
    assert forms.fields_needed(brokers, "US")[ProfileField.POSTAL_ADDRESS] == 0
