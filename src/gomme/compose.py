"""Compose the text of a removal request. Pure: no I/O besides reading the templates.

The request is written in the broker's language when a template exists for it, in
English otherwise. It identifies the user with the broker's `required_fields` only.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from functools import cache

import jinja2

from gomme.brokers.fields import ProfileField
from gomme.brokers.schema import Broker
from gomme.legal.statutes import Jurisdiction, jurisdictions
from gomme.profile import MissingProfileDataError, Profile

# GDPR applies to residents of the EU and of the other EEA states (Iceland,
# Liechtenstein, Norway). TODO(verify): the UK GDPR, with its own authority (ICO).
EU_EEA = frozenset(
    {
        "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU",
        "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK",
        "IS", "LI", "NO",
    }
)  # fmt: skip

# Supervisory authority named in the request (art. 77), by country of residence.
AUTHORITIES = {
    "FR": (
        "de la CNIL",
        "the CNIL, the French data protection authority",
    ),
}
DEFAULT_AUTHORITY = ("de l'autorité de contrôle compétente", "the competent supervisory authority")

ENGLISH_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip

REMINDER_TEMPLATES = {
    (Jurisdiction.GDPR, "fr"): "fr/gdpr_reminder.txt.j2",
    (Jurisdiction.GDPR, "en"): "en/gdpr_reminder.txt.j2",
    (Jurisdiction.CCPA, "en"): "en/ccpa_reminder.txt.j2",
}

TEMPLATES = {
    (Jurisdiction.GDPR, "fr"): "fr/gdpr.txt.j2",
    (Jurisdiction.GDPR, "en"): "en/gdpr.txt.j2",
    (Jurisdiction.CCPA, "en"): "en/ccpa.txt.j2",
}

LABELS = {
    "fr": {
        ProfileField.FULL_NAME: "Nom",
        ProfileField.EMAIL: "Adresse email",
        ProfileField.POSTAL_ADDRESS: "Adresse postale",
        ProfileField.BIRTH_DATE: "Date de naissance",
        ProfileField.PHONE: "Téléphone",
    },
    "en": {
        ProfileField.FULL_NAME: "Name",
        ProfileField.EMAIL: "Email address",
        ProfileField.POSTAL_ADDRESS: "Postal address",
        ProfileField.BIRTH_DATE: "Date of birth",
        ProfileField.PHONE: "Phone",
    },
}


# Labels of the other identifiers the user declared (old emails, phones, addresses).
OTHER_LABELS = {
    "fr": {
        ProfileField.EMAIL: "Autres adresses email utilisées",
        ProfileField.POSTAL_ADDRESS: "Anciennes adresses postales",
        ProfileField.PHONE: "Autres numéros de téléphone",
    },
    "en": {
        ProfileField.EMAIL: "Other email addresses used",
        ProfileField.POSTAL_ADDRESS: "Previous postal addresses",
        ProfileField.PHONE: "Other phone numbers",
    },
}


class NoApplicableLawError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RequestText:
    subject: str
    body: str
    language: str
    jurisdiction: Jurisdiction


def choose_jurisdiction(profile: Profile, broker: Broker) -> Jurisdiction:
    """GDPR for a resident of the EU/EEA when the broker is subject to it, else CCPA."""
    available = jurisdictions(broker.legal_basis)
    if profile.country in EU_EEA and Jurisdiction.GDPR in available:
        return Jurisdiction.GDPR
    if Jurisdiction.CCPA in available:
        return Jurisdiction.CCPA
    if Jurisdiction.GDPR in available:  # e.g. a European broker: GDPR art. 3.1
        return Jurisdiction.GDPR
    # Unreachable while every legal basis belongs to GDPR or CCPA (the schema requires one).
    raise NoApplicableLawError(  # pragma: no cover
        f"aucune base légale utilisable pour {broker.slug}"
    )


def identity_lines(
    profile: Profile, fields: Sequence[ProfileField], language: str
) -> list[tuple[str, str]]:
    """(label, value) for each field the broker needs, and only those."""
    missing = profile.missing(fields)
    if missing:
        raise MissingProfileDataError(missing)
    labels = LABELS[language]
    lines: list[tuple[str, str]] = []
    for field in ProfileField:  # stable order, whatever the definition's order
        if field not in fields:
            continue
        match field:
            case ProfileField.FULL_NAME:
                value = profile.full_name
            case ProfileField.EMAIL:
                value = profile.emails_to_erase[0]
            case ProfileField.POSTAL_ADDRESS:
                assert profile.postal_address is not None  # noqa: S101 - checked above
                value = profile.postal_address.one_line()
            case ProfileField.BIRTH_DATE:
                assert profile.birth_date is not None  # noqa: S101 - checked above
                born = profile.birth_date
                value = born.strftime("%d/%m/%Y") if language == "fr" else born.isoformat()
            case ProfileField.PHONE:
                assert profile.phone is not None  # noqa: S101 - checked above
                value = profile.phone
        lines.append((labels[field], value))
        others = other_identifiers(profile, field)
        if others:
            lines.append((OTHER_LABELS[language][field], " ; ".join(others)))
    return lines


def other_identifiers(profile: Profile, field: ProfileField) -> tuple[str, ...]:
    """The user's other values of `field` (old emails, phones, addresses), as text."""
    match field:
        case ProfileField.EMAIL:
            return profile.emails_to_erase[1:]
        case ProfileField.PHONE:
            return profile.other_phones
        case ProfileField.POSTAL_ADDRESS:
            return tuple(address.one_line() for address in profile.previous_addresses)
        case _:
            return ()


def covers_others(profile: Profile, fields: Sequence[ProfileField]) -> bool:
    """Whether the request lists other identifiers than the current ones."""
    return any(other_identifiers(profile, field) for field in fields)


@cache
def template_environment() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.PackageLoader("gomme", "templates"),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,  # noqa: S701 - plain-text emails and letters, not HTML
    )


def compose(
    broker: Broker,
    profile: Profile,
    *,
    jurisdiction: Jurisdiction | None = None,
    access: bool = False,
    notify_recipients: bool = True,
) -> RequestText:
    jurisdiction = jurisdiction or choose_jurisdiction(profile, broker)
    language = broker.language if (jurisdiction, broker.language) in TEMPLATES else "en"
    authority_fr, authority_en = AUTHORITIES.get(profile.country, DEFAULT_AUTHORITY)
    address = profile.postal_address
    text = (
        template_environment()
        .get_template(TEMPLATES[jurisdiction, language])
        .render(
            broker_name=broker.name,
            full_name=profile.full_name,
            identity=identity_lines(profile, broker.required_fields, language),
            covers_others=covers_others(profile, broker.required_fields),
            reply_to=None if profile.contact_is_identifier else profile.email,
            access=access,
            notify_recipients=notify_recipients,
            authority_fr=authority_fr,
            authority_en=authority_en,
            california_resident=address is not None
            and address.country == "US"
            and address.state == "CA",
        )
    )
    subject, _, body = text.strip().partition("\n\n")
    return RequestText(subject.strip(), body.strip() + "\n", language, jurisdiction)


def compose_reminder(
    broker: Broker,
    profile: Profile,
    jurisdiction: Jurisdiction,
    *,
    sent_on: date,
    due: date,
    acknowledged: bool,
    extended: bool = False,
) -> RequestText:
    """A reminder for a request left unanswered past its legal deadline."""
    language = broker.language if (jurisdiction, broker.language) in REMINDER_TEMPLATES else "en"
    authority_fr, authority_en = AUTHORITIES.get(profile.country, DEFAULT_AUTHORITY)
    address = profile.postal_address

    def written(day: date) -> str:
        if language == "fr":
            return day.strftime("%d/%m/%Y")
        return f"{ENGLISH_MONTHS[day.month - 1]} {day.day}, {day.year}"  # not locale-dependent

    text = (
        template_environment()
        .get_template(REMINDER_TEMPLATES[jurisdiction, language])
        .render(
            sent_on=written(sent_on),
            due=written(due),
            acknowledged=acknowledged,
            extended=extended,
            full_name=profile.full_name,
            identity=identity_lines(profile, broker.required_fields, language),
            authority_fr=authority_fr,
            authority_en=authority_en,
            california_resident=address is not None
            and address.country == "US"
            and address.state == "CA",
        )
    )
    subject, _, body = text.strip().partition("\n\n")
    return RequestText(subject.strip(), body.strip() + "\n", language, jurisdiction)
