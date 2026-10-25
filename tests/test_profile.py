"""Tests for the user profile and the values brokers may receive."""

from datetime import date

import pydantic
import pytest

from gomme.brokers.fields import ProfileField, render
from gomme.profile import MissingProfileDataError, PostalAddress, Profile, placeholder_values

TODAY = date(2026, 10, 25)


def test_full_name_and_masked_summary(profile: Profile) -> None:
    assert profile.full_name == "Jeanne Exemple"
    summary = profile.masked()
    assert summary["nom"] == "J*** E***"
    assert summary["email de contact"] == "j***@e***.org"
    assert summary["emails à effacer"] == "j***@e***.org"
    assert "Exemple" not in str(summary)
    assert "rue" not in str(summary)


def test_only_allowed_fields_are_given(profile: Profile) -> None:
    values = placeholder_values(profile, {ProfileField.EMAIL}, TODAY)

    assert values == {"email": "jeanne.exemple@example.org"}


def test_name_placeholders(profile: Profile) -> None:
    values = placeholder_values(profile, {ProfileField.FULL_NAME}, TODAY)

    assert values["first_name"] == "Jeanne"
    assert values["name_slug"] == "jeanne-exemple"
    assert values["name_title_slug"] == "Jeanne-Exemple"


def test_us_address_placeholders() -> None:
    profile = Profile(
        first_name="Jane",
        last_name="Doe",
        email="jane@example.org",
        country="US",
        language="en",
        postal_address=PostalAddress(
            street="1 Example St", postal_code="00000", city="Nowhere", country="US", state="NY"
        ),
    )

    values = placeholder_values(profile, {ProfileField.POSTAL_ADDRESS}, TODAY)

    assert (values["state"], values["state_name"], values["state_slug"]) == (
        "NY",
        "New York",
        "New-York",
    )


@pytest.mark.parametrize(("born", "age"), [(date(1990, 10, 25), "36"), (date(1990, 10, 26), "35")])
def test_age_counts_the_birthday(profile: Profile, born: date, age: str) -> None:
    profile = profile.model_copy(update={"birth_date": born})

    assert placeholder_values(profile, {ProfileField.BIRTH_DATE}, TODAY)["age"] == age


def test_missing_profile_data_is_reported(profile: Profile) -> None:
    with pytest.raises(MissingProfileDataError, match="phone"):
        placeholder_values(profile, {ProfileField.EMAIL, ProfileField.PHONE}, TODAY)


def test_render_refuses_values_the_broker_may_not_receive(profile: Profile) -> None:
    values = placeholder_values(profile, {ProfileField.EMAIL}, TODAY)

    assert render("{email} {{x}}", values) == "jeanne.exemple@example.org {x}"
    with pytest.raises(KeyError, match="first_name"):
        render("{first_name}", values)


@pytest.mark.parametrize(
    "changes",
    [
        {"email": "pas-une-adresse"},
        {"language": "de"},
        {"phone": "appelez-moi"},
        {"birth_date": date(2999, 1, 1)},
        {"nickname": "x"},
    ],
)
def test_invalid_profiles_are_rejected(profile: Profile, changes: dict[str, object]) -> None:
    with pytest.raises(pydantic.ValidationError):
        Profile.model_validate({**profile.model_dump(), **changes})


def test_state_is_only_for_us_addresses() -> None:
    with pytest.raises(pydantic.ValidationError, match="État américain"):
        PostalAddress(street="x", postal_code="0", city="y", country="FR", state="NY")


def test_contact_address_is_not_always_an_identifier(profile: Profile) -> None:
    dedicated = profile.model_copy(
        update={"include_contact_email": False, "other_emails": ("ancienne@example.org",)}
    )

    assert dedicated.emails_to_erase == ("ancienne@example.org",)
    assert not dedicated.contact_is_identifier
    values = placeholder_values(dedicated, {ProfileField.EMAIL}, TODAY)
    assert values == {"email": "ancienne@example.org"}  # web forms get an identifier
    nothing = profile.model_copy(update={"include_contact_email": False})
    assert nothing.missing([ProfileField.EMAIL]) == (ProfileField.EMAIL,)
