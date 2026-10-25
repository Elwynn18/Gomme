"""The user's profile, and the values a broker may receive from it."""

from collections.abc import Collection, Mapping
from datetime import date
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gomme.brokers.fields import ProfileField
from gomme.brokers.schema import CountryCode, EmailAddress
from gomme.privacy import mask_email, mask_phone, mask_text

US_STATES: Mapping[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}

Language = Annotated[str, StringConstraints(pattern=r"^(fr|en)$")]
# One line of printable text: these values end up in emails and letters.
SingleLine = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$"
    ),
]
PhoneNumber = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^\+?[0-9 .()-]{6,20}$")
]


MAX_OTHERS = 5  # other emails, other phones, previous addresses: at most this many each


class MissingProfileDataError(ValueError):
    """The profile lacks a field a broker needs."""

    def __init__(self, missing: Collection[ProfileField]) -> None:
        self.missing = tuple(missing)
        super().__init__(
            "champ(s) du profil manquant(s) : " + ", ".join(field.value for field in self.missing)
        )


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PostalAddress(_StrictModel):
    street: SingleLine
    postal_code: SingleLine
    city: SingleLine
    country: CountryCode
    state: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")] | None = None  # US only

    @model_validator(mode="after")
    def _state_is_a_us_state(self) -> Self:
        if self.state is not None and (self.country != "US" or self.state not in US_STATES):
            raise ValueError("state : code d'État américain, seulement pour une adresse aux US")
        return self

    def one_line(self) -> str:
        parts = [self.street, f"{self.postal_code} {self.city}"]
        if self.state:
            parts.append(self.state)
        parts.append(self.country)
        return ", ".join(parts)


class Profile(_StrictModel):
    """Who the requests are made for: always the person running Gomme."""

    first_name: SingleLine
    last_name: SingleLine
    email: EmailAddress  # contact address: sends the requests and receives the answers
    country: CountryCode  # country of residence: decides GDPR or CCPA
    language: Language = "fr"
    postal_address: PostalAddress | None = None
    birth_date: date | None = None
    phone: PhoneNumber | None = None
    # Whether the contact address is also one whose data should be erased. When it is not
    # (an address kept for these requests only), it is never given as an identifier.
    include_contact_email: bool = True
    # Other identifiers the user used, whose data should be erased too. Each one only goes
    # to the brokers that identify people by that kind of field (minimisation).
    other_emails: Annotated[tuple[EmailAddress, ...], Field(max_length=MAX_OTHERS)] = ()
    other_phones: Annotated[tuple[PhoneNumber, ...], Field(max_length=MAX_OTHERS)] = ()
    previous_addresses: Annotated[tuple[PostalAddress, ...], Field(max_length=MAX_OTHERS)] = ()

    @field_validator("birth_date")
    @classmethod
    def _born_in_the_past(cls, value: date | None) -> date | None:
        if value is not None and value >= date.today():
            raise ValueError("la date de naissance doit être passée")
        return value

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def emails_to_erase(self) -> tuple[str, ...]:
        """The email addresses whose data the requests are about (not the contact
        address, unless the user said so)."""
        contact = (self.email,) if self.include_contact_email else ()
        others = tuple(e for e in self.other_emails if e.lower() != self.email.lower())
        return contact + others

    @property
    def contact_is_identifier(self) -> bool:
        return self.email.lower() in (e.lower() for e in self.emails_to_erase)

    def has(self, field: ProfileField) -> bool:
        match field:
            case ProfileField.FULL_NAME:
                return True
            case ProfileField.EMAIL:
                return bool(self.emails_to_erase)
            case ProfileField.POSTAL_ADDRESS:
                return self.postal_address is not None
            case ProfileField.BIRTH_DATE:
                return self.birth_date is not None
            case ProfileField.PHONE:
                return self.phone is not None

    def missing(self, fields: Collection[ProfileField]) -> tuple[ProfileField, ...]:
        return tuple(field for field in fields if not self.has(field))

    def masked(self) -> dict[str, str]:
        """A summary safe to print: every value is masked."""
        summary = {
            "nom": f"{mask_text(self.first_name)} {mask_text(self.last_name)}",
            "email de contact": mask_email(self.email),
            "pays": self.country,
            "langue": self.language,
        }
        if self.postal_address:
            summary["adresse"] = f"*** ({self.postal_address.country})"
        if self.birth_date:
            summary["date de naissance"] = "**/**/****"
        if self.phone:
            summary["téléphone"] = mask_phone(self.phone)
        summary["emails à effacer"] = (
            ", ".join(mask_email(e) for e in self.emails_to_erase) or "aucun"
        )
        if self.other_phones:
            summary["autres téléphones"] = ", ".join(mask_phone(p) for p in self.other_phones)
        if self.previous_addresses:
            summary["anciennes adresses"] = str(len(self.previous_addresses))
        return summary


def placeholder_values(
    profile: Profile, allowed: Collection[ProfileField], today: date
) -> dict[str, str]:
    """Values of the placeholders of the `allowed` fields only (minimisation).

    Raises MissingProfileDataError if the profile lacks one of them.
    """
    missing = profile.missing(allowed)
    if missing:
        raise MissingProfileDataError(missing)
    values: dict[str, str] = {}
    if ProfileField.FULL_NAME in allowed:
        slug = "-".join(
            "".join(char for char in word if char.isalnum())
            for word in profile.full_name.lower().split()
        )
        values |= {
            "full_name": profile.full_name,
            "first_name": profile.first_name,
            "last_name": profile.last_name,
            "name_slug": slug,
            "name_title_slug": "-".join(part.capitalize() for part in slug.split("-")),
        }
    if ProfileField.EMAIL in allowed:
        values["email"] = profile.emails_to_erase[0]  # an identifier, not the contact
    if ProfileField.POSTAL_ADDRESS in allowed and profile.postal_address is not None:
        address = profile.postal_address
        state_name = US_STATES.get(address.state or "", "")
        values |= {
            "street": address.street,
            "city": address.city,
            "postal_code": address.postal_code,
            "state": address.state or "",
            "state_name": state_name,
            "state_slug": state_name.replace(" ", "-"),
        }
    if ProfileField.BIRTH_DATE in allowed and profile.birth_date is not None:
        born = profile.birth_date
        age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        values |= {
            "birth_date": born.isoformat(),
            "birth_year": str(born.year),
            "age": str(age),
        }
    if ProfileField.PHONE in allowed and profile.phone is not None:
        values["phone"] = profile.phone
    return values
