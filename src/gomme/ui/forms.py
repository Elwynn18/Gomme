"""Profile forms of the web interface: the first-run wizard and the profile page.

Pure functions: they turn submitted form values into a Config, with error messages in
plain French keyed by field. Messages never repeat the values typed (personal data).
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date

from pydantic import TypeAdapter, ValidationError

from gomme.brokers.fields import ProfileField
from gomme.brokers.schema import Broker, EmailAddress
from gomme.compose import EU_EEA
from gomme.config import Composer, Config, EmailConfig
from gomme.profile import US_STATES, PhoneNumber, PostalAddress, Profile, SingleLine
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
    "composer",
)
ADDRESS_FIELDS = ("street", "postal_code", "city", "state")
MAX_LENGTH = 300

GMAIL_DOMAINS = ("gmail.com", "googlemail.com")


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
    Step("sending", "Envoi", ("composer",)),
    Step("review", "Vérification", ()),
)
STEP_NAMES = tuple(step.name for step in STEPS)


def step_named(name: str) -> Step:
    return next((step for step in STEPS if step.name == name), STEPS[0])


def next_step(step: Step, offset: int) -> Step:
    index = STEPS.index(step) + offset
    return STEPS[max(0, min(index, len(STEPS) - 1))]


def clean(form: Mapping[str, str]) -> dict[str, str]:
    """The form values Gomme knows, trimmed; everything else is dropped."""
    return {name: form.get(name, "").strip()[:MAX_LENGTH] for name in FIELDS}


def suggested_composer(email: str) -> Composer:
    domain = email.rsplit("@", 1)[-1].lower()
    return Composer.GMAIL if domain in GMAIL_DOMAINS else Composer.MAIL_APP


def values_of(config: Config) -> dict[str, str]:
    """The form values of an existing configuration, to edit it."""
    profile = config.profile
    address = profile.postal_address
    return {
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
        "composer": config.email.composer.value,
    }


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
    if "composer" in wanted and values.get("composer") not in {c.value for c in Composer}:
        errors["composer"] = "Choisissez une façon d'envoyer vos emails."
    return errors


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


def build_config(values: Mapping[str, str], base: Config | None = None) -> Config:
    """The configuration from valid form values, keeping the rest of `base` (SMTP
    settings, request options...). Call `errors_for` first: this raises ValueError."""
    country = values["country"]
    address = None
    if values.get("street"):
        address = PostalAddress(
            street=values["street"],
            postal_code=values["postal_code"],
            city=values["city"],
            country=country,
            state=(values.get("state") or None) if country == "US" else None,
        )
    profile = Profile(
        first_name=values["first_name"],
        last_name=values["last_name"],
        email=values["email"],
        country=country,
        language=base.profile.language if base else "fr",
        postal_address=address,
        birth_date=date.fromisoformat(values["birth_date"]) if values.get("birth_date") else None,
        phone=values.get("phone") or None,
    )
    composer = Composer(values.get("composer") or suggested_composer(values["email"]))
    if base is None:
        return Config(profile=profile, email=EmailConfig(composer=composer))
    email = base.email.model_copy(update={"composer": composer})
    return base.model_copy(update={"profile": profile, "email": email})


def fields_needed(brokers: Iterable[Broker], country: str) -> dict[ProfileField, int]:
    """How many brokers of the default selection need each optional field."""
    _, selected = selection_for_country(brokers, country)
    optional = (ProfileField.POSTAL_ADDRESS, ProfileField.BIRTH_DATE, ProfileField.PHONE)
    return {
        field: sum(1 for broker in selected if field in broker.required_fields)
        for field in optional
    }
