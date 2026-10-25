"""Profile fields a broker may receive, and the placeholders web form steps may use.

A web form step can only insert profile data through a placeholder from the closed list
below. Each placeholder reveals one profile field, so the schema can check that a broker
definition declares, in `required_fields`, every field its steps would actually send.
"""

from collections.abc import Mapping
from enum import StrEnum
from string import Formatter
from types import MappingProxyType


class ProfileField(StrEnum):
    """Profile fields a broker may need to identify the user."""

    FULL_NAME = "full_name"
    EMAIL = "email"
    POSTAL_ADDRESS = "postal_address"
    BIRTH_DATE = "birth_date"
    PHONE = "phone"


LISTING_URL = "listing_url"
"""Runtime value: the URL of the user's own listing, picked by the user in a `search` step."""

PLACEHOLDERS: Mapping[str, ProfileField | None] = MappingProxyType(
    {
        "full_name": ProfileField.FULL_NAME,
        "first_name": ProfileField.FULL_NAME,
        "last_name": ProfileField.FULL_NAME,
        "name_slug": ProfileField.FULL_NAME,  # jane-doe
        "name_title_slug": ProfileField.FULL_NAME,  # Jane-Doe
        "email": ProfileField.EMAIL,
        "street": ProfileField.POSTAL_ADDRESS,
        "city": ProfileField.POSTAL_ADDRESS,
        "postal_code": ProfileField.POSTAL_ADDRESS,
        "state": ProfileField.POSTAL_ADDRESS,  # US state code: NY
        "state_name": ProfileField.POSTAL_ADDRESS,  # New York
        "state_slug": ProfileField.POSTAL_ADDRESS,  # New-York
        "birth_date": ProfileField.BIRTH_DATE,
        "birth_year": ProfileField.BIRTH_DATE,
        "age": ProfileField.BIRTH_DATE,
        "phone": ProfileField.PHONE,
        LISTING_URL: None,
    }
)
"""Every placeholder a step may use, with the profile field it reveals (None: not profile data)."""


def placeholders_in(template: str) -> frozenset[str]:
    """Return the placeholders used in `template`.

    Raises ValueError for malformed braces, unknown names, and any attribute access,
    indexing, conversion or format spec: a definition must not reach anything else.
    Literal braces are written ``{{`` and ``}}``.
    """
    try:
        parsed = list(Formatter().parse(template))
    except ValueError:
        raise ValueError("accolades mal formées (écrire {{ et }} pour une accolade)") from None

    names: set[str] = set()
    for _literal, name, spec, conversion in parsed:
        if name is None:
            continue
        if name not in PLACEHOLDERS:
            known = ", ".join(f"{{{known}}}" for known in PLACEHOLDERS)
            raise ValueError(f"variable inconnue {{{name}}} (variables possibles : {known})")
        if spec or conversion:
            raise ValueError(f"la variable {{{name}}} ne prend ni format ni conversion")
        names.add(name)
    return frozenset(names)


class MissingValueError(KeyError):
    """A template uses a placeholder with no value (a field the broker may not receive)."""


def render(template: str, values: Mapping[str, str]) -> str:
    """Fill the placeholders of a validated template; literal ``{{`` become ``{``."""
    missing = placeholders_in(template) - values.keys()
    if missing:
        raise MissingValueError(", ".join(sorted(missing)))
    return template.format_map(values)
