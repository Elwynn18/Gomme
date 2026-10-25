"""Profile forms of the web interface: the first-run wizard and the profile page.

Pure functions: they turn submitted form values into a Config, with error messages in
plain French keyed by field. Messages never repeat the values typed (personal data).
"""

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date

from pydantic import TypeAdapter, ValidationError

from gomme.brokers.fields import ProfileField
from gomme.brokers.schema import Broker, EmailAddress
from gomme.compose import EU_EEA
from gomme.config import (
    DEFAULT_PASSWORD_ENV,
    Composer,
    Config,
    EmailConfig,
    EmailMode,
    SmtpConfig,
    SmtpSecurity,
)
from gomme.profile import (
    MAX_OTHERS,
    US_STATES,
    PhoneNumber,
    PostalAddress,
    Profile,
    SingleLine,
)
from gomme.workflow import selection_for_country

# Countries offered in the form: the EU/EEA (GDPR), then a few common others.
COUNTRIES: Mapping[str, str] = {
    "FR": "France",
    "DE": "Allemagne",
    "AT": "Autriche",
    "BE": "Belgique",
    "BG": "Bulgarie",
    "CY": "Chypre",
    "HR": "Croatie",
    "DK": "Danemark",
    "ES": "Espagne",
    "EE": "Estonie",
    "FI": "Finlande",
    "GR": "Grèce",
    "HU": "Hongrie",
    "IE": "Irlande",
    "IS": "Islande",
    "IT": "Italie",
    "LV": "Lettonie",
    "LI": "Liechtenstein",
    "LT": "Lituanie",
    "LU": "Luxembourg",
    "MT": "Malte",
    "NO": "Norvège",
    "NL": "Pays-Bas",
    "PL": "Pologne",
    "PT": "Portugal",
    "CZ": "République tchèque",
    "RO": "Roumanie",
    "SK": "Slovaquie",
    "SI": "Slovénie",
    "SE": "Suède",
    "CH": "Suisse",
    "GB": "Royaume-Uni",
    "CA": "Canada",
    "US": "États-Unis",
}
assert COUNTRIES.keys() >= EU_EEA  # noqa: S101 - every GDPR country can be chosen

PREVIOUS_SLOTS = 3  # previous postal addresses offered by the form
PREVIOUS_PARTS = ("street", "postal_code", "city")
PREVIOUS_FIELDS = tuple(
    f"prev_{part}_{slot}" for slot in range(PREVIOUS_SLOTS) for part in PREVIOUS_PARTS
)
SMTP_FIELDS = ("smtp_host", "smtp_port", "smtp_security", "smtp_username")
LIST_FIELDS = ("other_emails", "other_phones")

FIELDS = (
    "first_name",
    "last_name",
    "email",
    "country",
    "street",
    "postal_code",
    "city",
    "state",
    "birth_date",
    "phone",
    *LIST_FIELDS,
    *PREVIOUS_FIELDS,
    "sending",
    *SMTP_FIELDS,
)
ADDRESS_FIELDS = ("street", "postal_code", "city", "state")
MAX_LENGTH = 300
MAX_LIST_LENGTH = 2000

# Sending: open each email in Gmail or in the mail application, or send it through SMTP.
SENDING_CHOICES = ("gmail", "mail_app", "smtp")
GMAIL_DOMAINS = ("gmail.com", "googlemail.com")
# Gmail's SMTP server. It needs an app password (two-step verification turned on).
GMAIL_SMTP = {"smtp_host": "smtp.gmail.com", "smtp_port": "587", "smtp_security": "starttls"}
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST = re.compile(rf"^{_LABEL}(?:\.{_LABEL})+$")


@dataclass(frozen=True, slots=True)
class Step:
    name: str
    title: str
    fields: tuple[str, ...]


STEPS = (
    Step("welcome", "Bienvenue", ()),
    Step("identity", "Vous", ("first_name", "last_name", "email", "country")),
    Step(
        "details",
        "Vous retrouver",
        ("street", "postal_code", "city", "state", "birth_date", "phone"),
    ),
    Step("others", "Anciennes coordonnées", (*LIST_FIELDS, *PREVIOUS_FIELDS)),
    Step("sending", "Envoi", ("sending", *SMTP_FIELDS)),
    Step("review", "Vérification", ()),
)


def step_named(name: str) -> Step:
    return next((step for step in STEPS if step.name == name), STEPS[0])


def next_step(step: Step, offset: int) -> Step:
    index = STEPS.index(step) + offset
    return STEPS[max(0, min(index, len(STEPS) - 1))]


def clean(form: Mapping[str, str]) -> dict[str, str]:
    """The form values Gomme knows, trimmed; everything else is dropped."""
    return {
        name: form.get(name, "").strip()[: MAX_LIST_LENGTH if name in LIST_FIELDS else MAX_LENGTH]
        for name in FIELDS
    }


def is_gmail(email: str) -> bool:
    return email.rsplit("@", 1)[-1].lower() in GMAIL_DOMAINS


def suggested_composer(email: str) -> Composer:
    return Composer.GMAIL if is_gmail(email) else Composer.MAIL_APP


def with_sending_defaults(values: Mapping[str, str]) -> dict[str, str]:
    """Pre-select the sending choice, and Gmail's SMTP settings for a Gmail address."""
    filled = dict(values)
    email = filled.get("email", "")
    if not filled.get("sending"):
        filled["sending"] = suggested_composer(email).value
    if not filled.get("smtp_host") and is_gmail(email):
        filled |= GMAIL_SMTP
    filled["smtp_username"] = filled.get("smtp_username") or email
    filled["smtp_port"] = filled.get("smtp_port") or "587"
    filled["smtp_security"] = filled.get("smtp_security") or SmtpSecurity.STARTTLS.value
    return filled


def _unique(items: Iterable[str], key: Callable[[str], str]) -> list[str]:
    result: list[str] = []
    for item in items:
        item = item.strip()
        if item and key(item) not in {key(seen) for seen in result}:
            result.append(item)
    return result


def split_emails(text: str) -> list[str]:
    """One address per line (commas and semicolons also separate), duplicates dropped."""
    return _unique(re.split(r"[\r\n;,]+", text), str.lower)


def split_phones(text: str) -> list[str]:
    """Phones contain spaces and dots: only new lines and semicolons separate them."""
    return _unique(re.split(r"[\r\n;]+", text), lambda phone: re.sub(r"\D", "", phone))


def values_of(config: Config) -> dict[str, str]:
    """The form values of an existing configuration, to edit it."""
    profile = config.profile
    address = profile.postal_address
    values = {
        "first_name": profile.first_name,
        "last_name": profile.last_name,
        "email": profile.email,
        "country": profile.country,
        "street": address.street if address else "",
        "postal_code": address.postal_code if address else "",
        "city": address.city if address else "",
        "state": (address.state or "") if address else "",
        "birth_date": profile.birth_date.isoformat() if profile.birth_date else "",
        "phone": profile.phone or "",
        "other_emails": "\n".join(profile.other_emails),
        "other_phones": "\n".join(profile.other_phones),
        "sending": "smtp" if config.email.mode is EmailMode.SMTP else config.email.composer,
    }
    for slot, previous in enumerate(profile.previous_addresses[:PREVIOUS_SLOTS]):
        values |= {
            f"prev_street_{slot}": previous.street,
            f"prev_postal_code_{slot}": previous.postal_code,
            f"prev_city_{slot}": previous.city,
        }
    smtp = config.email.smtp
    if smtp is not None:
        values |= {
            "smtp_host": smtp.host,
            "smtp_port": str(smtp.port),
            "smtp_security": smtp.security.value,
            "smtp_username": smtp.username,
        }
    return clean(values)


_single_line: TypeAdapter[str] = TypeAdapter(SingleLine)
_email: TypeAdapter[str] = TypeAdapter(EmailAddress)
_phone: TypeAdapter[str] = TypeAdapter(PhoneNumber)


def _valid(adapter: TypeAdapter[str], value: str) -> bool:
    try:
        adapter.validate_python(value)
    except ValidationError:
        return False
    return True


def errors_for(values: Mapping[str, str], fields: Iterable[str], today: date) -> dict[str, str]:
    """Problems with the given fields, as {field: message}. Empty when all is well."""
    wanted = set(fields)
    errors: dict[str, str] = {}
    for name in ("first_name", "last_name"):
        if name in wanted:
            if not values.get(name):
                errors[name] = "Ce champ est obligatoire."
            elif not _valid(_single_line, values[name]):
                errors[name] = "Une seule ligne, 200 caractères au plus."
    if "email" in wanted:
        if not values.get("email"):
            errors["email"] = "Ce champ est obligatoire."
        elif not _valid(_email, values["email"]):
            errors["email"] = "Cette adresse email ne semble pas valide."
    if "country" in wanted and values.get("country") not in COUNTRIES:
        errors["country"] = "Choisissez votre pays dans la liste."
    if wanted & set(ADDRESS_FIELDS):
        errors |= _address_errors(values)
    if "birth_date" in wanted and values.get("birth_date"):
        try:
            born = date.fromisoformat(values["birth_date"])
        except ValueError:
            errors["birth_date"] = "Date invalide."
        else:
            if not date(1900, 1, 1) <= born < today:
                errors["birth_date"] = "Cette date de naissance ne semble pas possible."
    if "phone" in wanted and values.get("phone") and not _valid(_phone, values["phone"]):
        errors["phone"] = "Uniquement des chiffres, des espaces et éventuellement un +."
    if "other_emails" in wanted:
        emails = split_emails(values.get("other_emails", ""))
        errors |= _list_errors("other_emails", emails, _email, "adresse")
    if "other_phones" in wanted:
        phones = split_phones(values.get("other_phones", ""))
        errors |= _list_errors("other_phones", phones, _phone, "numéro")
    if wanted & set(PREVIOUS_FIELDS):
        errors |= _previous_errors(values)
    if "sending" in wanted:
        if values.get("sending") not in SENDING_CHOICES:
            errors["sending"] = "Choisissez une façon d'envoyer vos emails."
        elif values.get("sending") == "smtp":
            errors |= _smtp_errors(values)
    return errors


def _list_errors(
    name: str, items: list[str], adapter: TypeAdapter[str], noun: str
) -> dict[str, str]:
    if len(items) > MAX_OTHERS:
        return {name: f"{MAX_OTHERS} au plus."}
    for index, item in enumerate(items):
        if not _valid(adapter, item):
            position = "La première" if index == 0 else f"La {index + 1}e"
            return {name: f"{position} {noun} de la liste ne semble pas valide."}
    return {}


def _address_errors(values: Mapping[str, str]) -> dict[str, str]:
    us = values.get("country") == "US"
    given = [name for name in ADDRESS_FIELDS if values.get(name)]
    if not given or (given == ["state"] and not us):
        return {}  # no address at all: allowed, it is optional
    errors: dict[str, str] = {}
    required = ("street", "postal_code", "city", *(("state",) if us else ()))
    for name in required:
        if not values.get(name):
            errors[name] = "À remplir aussi, pour une adresse complète."
    for name in ("street", "postal_code", "city"):
        if values.get(name) and not _valid(_single_line, values[name]):
            errors[name] = "Une seule ligne, 200 caractères au plus."
    if us and values.get("state") and values["state"] not in US_STATES:
        errors["state"] = "Choisissez un État dans la liste."
    return errors


def _previous_errors(values: Mapping[str, str]) -> dict[str, str]:
    errors: dict[str, str] = {}
    for slot in range(PREVIOUS_SLOTS):
        names = [f"prev_{part}_{slot}" for part in PREVIOUS_PARTS]
        if not any(values.get(name) for name in names):
            continue
        for name in names:
            if not values.get(name):
                errors[name] = "À remplir aussi, pour une adresse complète."
            elif not _valid(_single_line, values[name]):
                errors[name] = "Une seule ligne, 200 caractères au plus."
    return errors


def _smtp_errors(values: Mapping[str, str]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not _HOST.match(values.get("smtp_host", "")):
        errors["smtp_host"] = "Nom de serveur invalide (exemple : smtp.gmail.com)."
    port = values.get("smtp_port", "")
    if not (port.isdigit() and 0 < int(port) < 65536):
        errors["smtp_port"] = "Un numéro de port, par exemple 587."
    if values.get("smtp_security") not in {s.value for s in SmtpSecurity}:
        errors["smtp_security"] = "Choisissez STARTTLS ou SSL."
    if not values.get("smtp_username") or not _valid(_single_line, values["smtp_username"]):
        errors["smtp_username"] = "Ce champ est obligatoire."
    return errors


def _previous_addresses(values: Mapping[str, str], country: str) -> tuple[PostalAddress, ...]:
    return tuple(
        PostalAddress(
            street=values[f"prev_street_{slot}"],
            postal_code=values[f"prev_postal_code_{slot}"],
            city=values[f"prev_city_{slot}"],
            country=country,
        )
        for slot in range(PREVIOUS_SLOTS)
        if values.get(f"prev_street_{slot}")
    )


def build_config(values: Mapping[str, str], base: Config | None = None) -> Config:
    """The configuration from valid form values, keeping the rest of `base` (request
    options, limits...). Call `errors_for` first: this raises ValueError."""
    country = values["country"]
    email = values["email"]
    address = None
    if values.get("street"):
        address = PostalAddress(
            street=values["street"],
            postal_code=values["postal_code"],
            city=values["city"],
            country=country,
            state=(values.get("state") or None) if country == "US" else None,
        )
    phone = values.get("phone") or None
    digits = re.sub(r"\D", "", phone or "")
    profile = Profile(
        first_name=values["first_name"],
        last_name=values["last_name"],
        email=email,
        country=country,
        language=base.profile.language if base else "fr",
        postal_address=address,
        birth_date=date.fromisoformat(values["birth_date"]) if values.get("birth_date") else None,
        phone=phone,
        other_emails=tuple(
            other
            for other in split_emails(values.get("other_emails", ""))
            if other.lower() != email.lower()
        ),
        other_phones=tuple(
            other
            for other in split_phones(values.get("other_phones", ""))
            if re.sub(r"\D", "", other) != digits
        ),
        previous_addresses=_previous_addresses(values, country),
    )
    sending = values.get("sending") or suggested_composer(email).value
    current = base.email if base else EmailConfig()
    smtp = current.smtp
    if sending == "smtp":
        smtp = SmtpConfig(
            host=values["smtp_host"],
            port=int(values["smtp_port"]),
            security=SmtpSecurity(values["smtp_security"]),
            username=values["smtp_username"],
            password_env=smtp.password_env if smtp else DEFAULT_PASSWORD_ENV,
        )
        mode, composer = EmailMode.SMTP, suggested_composer(email)
    else:
        mode, composer = EmailMode.MANUAL, Composer(sending)
    email_config = current.model_copy(update={"mode": mode, "composer": composer, "smtp": smtp})
    if base is None:
        return Config(profile=profile, email=email_config)
    return base.model_copy(update={"profile": profile, "email": email_config})


def fields_needed(brokers: Iterable[Broker], country: str) -> dict[ProfileField, int]:
    """How many brokers of the default selection need each kind of field."""
    _, selected = selection_for_country(brokers, country)
    return {
        field: sum(1 for broker in selected if field in broker.required_fields)
        for field in ProfileField
    }
