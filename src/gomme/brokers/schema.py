"""Pydantic models for broker definitions (one YAML file per broker)."""

import re
from collections.abc import Iterator, Sized
from datetime import date
from enum import StrEnum
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StrictBool,
    StrictInt,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

from gomme.brokers.fields import LISTING_URL, PLACEHOLDERS, placeholders_in
from gomme.brokers.fields import ProfileField as ProfileField

SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
IDENTIFIER_PATTERN = r"^[a-z0-9]+(?:_[a-z0-9]+)*$"
COUNTRY_PATTERN = r"^[A-Z]{2}$"
LANGUAGE_PATTERN = r"^[a-z]{2}$"
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
DOMAIN_PATTERN = rf"^(?:{_LABEL}\.)+[a-z][a-z0-9-]{{0,61}}[a-z0-9]$"
# Deliberately simple: catches typos without pulling in an email-validation dependency.
EMAIL_PATTERN = r"^[^@\s]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$"
MAX_TIMEOUT_MS = 600_000


class Region(StrEnum):
    FR = "fr"
    EU = "eu"
    US = "us"
    GLOBAL = "global"


class Category(StrEnum):
    MARKETING = "marketing"
    PEOPLE_SEARCH = "people-search"
    ANNUAIRE = "annuaire"
    BACKGROUND_CHECK = "background-check"
    AUTRE = "autre"


class LegalBasis(StrEnum):
    GDPR_ART15 = "gdpr_art15"  # right of access, including the source of the data
    GDPR_ART17 = "gdpr_art17"  # right to erasure
    GDPR_ART19 = "gdpr_art19"  # notification of recipients
    GDPR_ART21 = "gdpr_art21"  # right to object
    CCPA_1798_105 = "ccpa_1798_105"  # right to delete


class Source(StrEnum):
    MANUAL = "manual"
    ERASER = "eraser"
    OPTOUT = "optout"
    BADBOOL = "badbool"
    OPTERY = "optery"


class StepType(StrEnum):
    NAVIGATE = "navigate"
    SEARCH = "search"
    FORM_FILL = "form_fill"
    CLICK = "click"
    WAIT_FOR = "wait_for"
    PROMPT_USER = "prompt_user"
    PROMPT_USER_IF_PRESENT = "prompt_user_if_present"
    ASSERT = "assert"
    CAPTURE = "capture"


class InputMethod(StrEnum):
    """How a form field receives its value."""

    FILL = "fill"  # Playwright fill(); also selects an <option> in a <select>
    TYPE = "type"  # real key presses, for forms that ignore fill()
    JS = "js"  # native value setter + input/change events, for React controlled inputs


# Emptiness is checked by validators rather than Field(min_length=1): on tuples, pydantic
# counts only the items that validated, so one broken channel would also report
# a misleading "should have at least 1 item" error.
def _not_empty[T: Sized](items: T) -> T:
    if not items:
        raise ValueError("at least one item is required")
    return items


def _not_empty_no_duplicates[T](items: tuple[T, ...]) -> tuple[T, ...]:
    _not_empty(items)
    if len(set(items)) != len(items):
        raise ValueError("duplicate values are not allowed")
    return items


def _check_template(text: str) -> str:
    placeholders_in(text)
    return text


def _check_regex(pattern: str) -> str:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"expression régulière invalide ({exc.msg})") from None
    return pattern


def _check_https_template(url: str) -> str:
    """A step URL: https, with placeholders allowed only after the host."""
    if not url.startswith("https://"):
        raise ValueError("l'URL doit commencer par https://")
    if "{" in urlsplit(url).netloc or not urlsplit(url).hostname:
        raise ValueError("l'hôte de l'URL doit être écrit en clair, sans variable")
    return url


NonBlankStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Slug = Annotated[str, StringConstraints(pattern=SLUG_PATTERN)]
Identifier = Annotated[str, StringConstraints(pattern=IDENTIFIER_PATTERN)]
Domain = Annotated[str, StringConstraints(max_length=253, pattern=DOMAIN_PATTERN)]
CountryCode = Annotated[str, StringConstraints(pattern=COUNTRY_PATTERN)]
LanguageCode = Annotated[str, StringConstraints(pattern=LANGUAGE_PATTERN)]
EmailAddress = Annotated[str, StringConstraints(pattern=EMAIL_PATTERN)]
Template = Annotated[NonBlankStr, AfterValidator(_check_template)]
"""Text that may contain placeholders, such as ``{first_name}`` (see gomme.brokers.fields)."""
UrlTemplate = Annotated[Template, AfterValidator(_check_https_template)]
Regex = Annotated[NonBlankStr, AfterValidator(_check_regex)]
TimeoutMs = Annotated[StrictInt, Field(gt=0, le=MAX_TIMEOUT_MS)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _templates(model: BaseModel) -> Iterator[str]:
    """Every Template value in `model`, nested models included."""
    for name, value in model:
        if name in _NOT_TEMPLATES:
            continue
        if isinstance(value, BaseModel):
            yield from _templates(value)
        elif isinstance(value, dict):
            for item in value.values():
                yield from _templates(item)
        elif isinstance(value, str):
            yield value


# Fields holding plain strings rather than Templates: identifiers, enums, code and regexes,
# where braces have their own meaning.
_NOT_TEMPLATES = frozenset({"id", "type", "input", "js_condition", "url_matches"})


class LocalizedText(_StrictModel):
    """Text shown to the user, French first. At least one language is required."""

    fr: Template | None = None
    en: Template | None = None

    @model_validator(mode="after")
    def _at_least_one_language(self) -> Self:
        if self.fr is None and self.en is None:
            raise ValueError("au moins une langue est requise (fr ou en)")
        return self

    def get(self, language: str) -> str:
        """The text in `language`, or in the other language if it is missing."""
        preferred, fallback = (self.en, self.fr) if language == "en" else (self.fr, self.en)
        # The validator guarantees at least one non-blank text.
        return preferred or fallback or ""


class _Step(_StrictModel):
    id: Identifier
    label: LocalizedText | None = None
    optional: StrictBool = False  # a failure is reported but does not stop the run

    def placeholders(self) -> frozenset[str]:
        return frozenset(name for text in _templates(self) for name in placeholders_in(text))


class NavigateStep(_Step):
    type: Literal["navigate"]
    url: UrlTemplate | None = None  # defaults to the channel URL


class SearchStep(_Step):
    """Open a search for the user's own name; the user picks their listing ({listing_url})."""

    type: Literal["search"]
    url: UrlTemplate
    result_selector: Template


class FormField(_StrictModel):
    selector: Template
    value: Template
    input: InputMethod = InputMethod.FILL
    force: StrictBool = False  # fill hidden or covered elements


class FormFillStep(_Step):
    type: Literal["form_fill"]
    fields: Annotated[dict[Identifier, FormField], AfterValidator(_not_empty)]
    frame: Template | None = None  # selector of the iframe holding the form


class ClickStep(_Step):
    type: Literal["click"]
    selector: Template
    frame: Template | None = None


class WaitForStep(_Step):
    type: Literal["wait_for"]
    selector: Template | None = None
    js_condition: NonBlankStr | None = None  # JS predicate, () => boolean
    timeout_ms: TimeoutMs = 30_000
    message: LocalizedText | None = None
    frame: Template | None = None

    @model_validator(mode="after")
    def _exactly_one_condition(self) -> Self:
        if (self.selector is None) == (self.js_condition is None):
            raise ValueError("indiquer soit selector, soit js_condition")
        return self


class PromptUserStep(_Step):
    """Pause until the user has done something in the browser (CAPTCHA, confirmation...)."""

    type: Literal["prompt_user"]
    message: LocalizedText


class PromptUserIfPresentStep(_Step):
    """Like prompt_user, only when `selector` is on the page."""

    type: Literal["prompt_user_if_present"]
    selector: Template
    message: LocalizedText
    frame: Template | None = None


class AssertStep(_Step):
    type: Literal["assert"]
    selector: Template | None = None
    text_contains: Template | None = None
    url_matches: Regex | None = None
    js_condition: NonBlankStr | None = None

    @model_validator(mode="after")
    def _at_least_one_condition(self) -> Self:
        if all(
            value is None
            for value in (self.selector, self.text_contains, self.url_matches, self.js_condition)
        ):
            raise ValueError(
                "indiquer au moins une condition : selector, text_contains, url_matches "
                "ou js_condition"
            )
        return self


class CaptureStep(_Step):
    """Screenshot of the page, kept as proof of the request."""

    type: Literal["capture"]


WebFormStep = Annotated[
    NavigateStep
    | SearchStep
    | FormFillStep
    | ClickStep
    | WaitForStep
    | PromptUserStep
    | PromptUserIfPresentStep
    | AssertStep
    | CaptureStep,
    Field(discriminator="type"),
]


class EmailChannel(_StrictModel):
    type: Literal["email"]
    address: EmailAddress


class WebFormChannel(_StrictModel):
    """A web form. Without steps, Gomme opens the page and the user fills it in."""

    type: Literal["web_form"]
    url: HttpUrl
    steps: tuple[WebFormStep, ...] = ()

    @model_validator(mode="after")
    def _check_steps(self) -> Self:
        seen: set[str] = set()
        searched = False
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"identifiant d'étape en double : {step.id}")
            seen.add(step.id)
            if LISTING_URL in step.placeholders() and not searched:
                raise ValueError(
                    f"l'étape {step.id} utilise {{{LISTING_URL}}} avant toute étape search"
                )
            searched = searched or isinstance(step, SearchStep)
        return self

    def step_urls(self) -> Iterator[str]:
        """The URLs the browser is sent to: the form itself and every explicit step URL."""
        if self.steps:
            yield str(self.url)
        for step in self.steps:
            if isinstance(step, NavigateStep | SearchStep) and step.url is not None:
                yield step.url

    def profile_fields(self) -> dict[ProfileField, tuple[str, str]]:
        """Profile fields the steps send, each with a (step id, placeholder) that uses it."""
        used: dict[ProfileField, tuple[str, str]] = {}
        for step in self.steps:
            for name in sorted(step.placeholders()):
                field = PLACEHOLDERS[name]
                if field is not None:
                    used.setdefault(field, (step.id, name))
        return used


class PostalChannel(_StrictModel):
    type: Literal["postal"]
    address: NonBlankStr


Channel = Annotated[EmailChannel | WebFormChannel | PostalChannel, Field(discriminator="type")]


class Broker(_StrictModel):
    """A data broker and the ways to send it a removal request."""

    slug: Slug
    name: NonBlankStr
    domain: Domain
    country: CountryCode | None = Field(default=None, validate_default=True)
    region: Region
    category: Category
    channels: Annotated[tuple[Channel, ...], AfterValidator(_not_empty)]
    legal_basis: Annotated[tuple[LegalBasis, ...], AfterValidator(_not_empty_no_duplicates)]
    required_fields: Annotated[tuple[ProfileField, ...], AfterValidator(_not_empty_no_duplicates)]
    requires_id: StrictBool
    language: LanguageCode
    source: Source
    source_ref: NonBlankStr
    last_verified: date | None = Field(default=None, validate_default=True)
    notes: str = ""

    @field_validator("channels")
    @classmethod
    def _automated_forms_stay_on_the_broker_domain(
        cls, channels: tuple[Channel, ...], info: ValidationInfo
    ) -> tuple[Channel, ...]:
        # `domain` is declared earlier, so it is already validated (absent if invalid).
        domain = info.data.get("domain")
        if domain is None:
            return channels
        for channel in channels:
            if not isinstance(channel, WebFormChannel):
                continue
            for url in channel.step_urls():
                host = (urlsplit(url).hostname or "").lower()
                if not url.startswith("https://") or not (
                    host == domain or host.endswith(f".{domain}")
                ):
                    raise ValueError(
                        f"un formulaire automatisé ne peut ouvrir que des pages https de "
                        f"{domain} : {host or url} refusé"
                    )
        return channels

    @field_validator("required_fields")
    @classmethod
    def _steps_only_send_declared_fields(
        cls, fields: tuple[ProfileField, ...], info: ValidationInfo
    ) -> tuple[ProfileField, ...]:
        channels: tuple[Channel, ...] = info.data.get("channels", ())
        for channel in channels:
            if not isinstance(channel, WebFormChannel):
                continue
            for field, (step_id, name) in channel.profile_fields().items():
                if field not in fields:
                    raise ValueError(
                        f"« {field} » manque alors que l'étape {step_id} l'envoie "
                        f"(variable {{{name}}})"
                    )
        return fields

    @field_validator("last_verified")
    @classmethod
    def _manual_entries_must_be_verified(
        cls, value: date | None, info: ValidationInfo
    ) -> date | None:
        # `source` is declared earlier, so it is already validated (absent if invalid).
        if value is None and info.data.get("source") is Source.MANUAL:
            raise ValueError("required when source is 'manual'")
        return value

    @model_validator(mode="after")
    def _manual_entries_must_have_a_country(self) -> Self:
        if self.country is None and self.source is Source.MANUAL:
            raise ValueError("country : obligatoire quand source vaut « manual »")
        return self
