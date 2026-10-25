"""Tests for the closed list of placeholders usable in web form steps."""

import pytest

from gomme.brokers.fields import LISTING_URL, PLACEHOLDERS, ProfileField, placeholders_in


def test_text_without_placeholder() -> None:
    assert placeholders_in("button[type='submit']") == frozenset()


def test_placeholders_are_collected() -> None:
    assert placeholders_in("{first_name} {last_name}, {city} {first_name}") == {
        "first_name",
        "last_name",
        "city",
    }


def test_doubled_braces_are_literal() -> None:
    assert placeholders_in("{{not_a_placeholder}} {email}") == {"email"}


@pytest.mark.parametrize(
    "template",
    [
        "{profile}",
        "{profile.legal_name}",  # attribute access, as OptOut allows
        "{email.__class__}",
        "{phone[0]}",
        "{}",
        "{0}",
    ],
)
def test_unknown_or_indirect_placeholders_are_rejected(template: str) -> None:
    with pytest.raises(ValueError, match="variable inconnue"):
        placeholders_in(template)


@pytest.mark.parametrize("template", ["{email!r}", "{age:>5}"])
def test_conversions_and_format_specs_are_rejected(template: str) -> None:
    with pytest.raises(ValueError, match="ni format ni conversion"):
        placeholders_in(template)


@pytest.mark.parametrize("template", ["{email", "email}", "{{email}"])
def test_unbalanced_braces_are_rejected(template: str) -> None:
    with pytest.raises(ValueError, match="accolades"):
        placeholders_in(template)


def test_every_profile_field_has_a_placeholder() -> None:
    assert set(PLACEHOLDERS.values()) - {None} == set(ProfileField)


def test_only_the_listing_url_is_not_profile_data() -> None:
    assert [name for name, field in PLACEHOLDERS.items() if field is None] == [LISTING_URL]
