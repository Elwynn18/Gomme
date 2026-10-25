"""What the dashboard shows: requests sorted by what the user has to do next.

Pure functions over tracker records and broker definitions, so they are easy to test.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from gomme.brokers.fields import ProfileField
from gomme.brokers.schema import Broker, Source
from gomme.compose import EU_EEA
from gomme.legal.deadlines import Deadline
from gomme.profile import Profile
from gomme.tracker.db import RequestRecord
from gomme.tracker.state import Status
from gomme.workflow import default_selection

# Colour family of each status badge (see the stylesheet).
STATUS_TONES = {
    Status.DRAFT: "muted",
    Status.QUEUED: "todo",
    Status.SENT: "wait",
    Status.ACKNOWLEDGED: "wait",
    Status.NEEDS_ACTION: "alert",
    Status.COMPLETED: "ok",
    Status.REFUSED: "alert",
    Status.OVERDUE: "alert",
    Status.ESCALATED: "escalated",
    Status.RECHECK_DUE: "todo",
}

MONTHS = (
    "janv.",
    "févr.",
    "mars",
    "avr.",
    "mai",
    "juin",
    "juil.",
    "août",
    "sept.",
    "oct.",
    "nov.",
    "déc.",
)


def french_date(day: date | None) -> str:
    if day is None:
        return "—"
    return f"{day.day} {MONTHS[day.month - 1]} {day.year}"


def plural(count: int, singular: str, several: str) -> str:
    """ "1 demande préparée", "3 demandes préparées"."""
    return f"{count} {singular if count <= 1 else several}"


def scope(country: str) -> str:
    """Where the brokers of the default selection operate, in words."""
    if country == "FR":
        return "en France ou dans le monde entier"
    if country == "US":
        return "aux États-Unis ou dans le monde entier"
    if country in EU_EEA:
        return "dans votre pays ou dans le monde entier"
    return "dans le monde entier"


def countdown(days_left: int) -> str:
    if days_left > 1:
        return f"encore {days_left} jours"
    if days_left == 1:
        return "dernier jour demain"
    if days_left == 0:
        return "dernier jour aujourd'hui"
    late = -days_left
    return f"délai dépassé de {late} jour{'s' if late > 1 else ''}"


@dataclass(frozen=True, slots=True)
class Row:
    request: RequestRecord
    broker: Broker | None
    deadline: Deadline | None
    days_left: int | None

    @property
    def name(self) -> str:
        return self.broker.name if self.broker else self.request.broker_slug


@dataclass
class Board:
    to_send: list[Row] = field(default_factory=list)
    needs_you: list[Row] = field(default_factory=list)
    problems: list[Row] = field(default_factory=list)
    waiting: list[Row] = field(default_factory=list)
    done: list[Row] = field(default_factory=list)
    drafts: int = 0

    @property
    def total(self) -> int:
        """Requests under way or finished (drafts left aside)."""
        return sum(
            len(rows)
            for rows in (self.to_send, self.needs_you, self.problems, self.waiting, self.done)
        )

    @property
    def progress(self) -> int:
        """Share of requests finished, in percent."""
        return round(100 * len(self.done) / self.total) if self.total else 0


def board(requests: Iterable[RequestRecord], brokers: Mapping[str, Broker], today: date) -> Board:
    result = Board()
    for request in requests:
        broker = brokers.get(request.broker_slug)
        deadline = request.deadline(broker)
        row = Row(request, broker, deadline, deadline.days_left(today) if deadline else None)
        match request.status:
            case Status.DRAFT:
                result.drafts += 1
            case Status.QUEUED:
                result.to_send.append(row)
            case Status.NEEDS_ACTION:
                result.needs_you.append(row)
            case Status.OVERDUE | Status.REFUSED | Status.RECHECK_DUE:
                result.problems.append(row)
            case Status.SENT | Status.ACKNOWLEDGED:
                result.waiting.append(row)
            case Status.COMPLETED | Status.ESCALATED:
                result.done.append(row)
    result.waiting.sort(key=lambda row: row.days_left if row.days_left is not None else 9999)
    return result


@dataclass(frozen=True, slots=True)
class Selection:
    """The brokers Gomme would write to, and those the profile is not complete enough for."""

    description: str
    total: int
    ready: int
    blocked: Mapping[ProfileField, int]  # missing field -> number of brokers waiting for it


def selection(
    brokers: Iterable[Broker], profile: Profile, already: Sequence[str] = ()
) -> Selection:
    """`already`: slugs that have a request; they are not counted again."""
    description, chosen = default_selection(brokers, profile)
    blocked: dict[ProfileField, int] = {}
    ready = 0
    for broker in chosen:
        if broker.slug in already:
            continue
        missing = profile.missing(broker.required_fields)
        if not missing:
            ready += 1
        for missing_field in missing:
            blocked[missing_field] = blocked.get(missing_field, 0) + 1
    return Selection(description, len(chosen), ready, blocked)


# --- what each broker receives, and why ---------------------------------------------------

_ERASER_ESTIMATE = "Eraser, d'où vient cette fiche, ne précise pas les informations demandées"


def field_reason(broker: Broker, field: ProfileField) -> str:
    """Why the broker needs `field`: from its definition, else said plainly per source."""
    reason = broker.fields_why.get(field)
    if reason:
        return reason
    match broker.source:
        case Source.OPTOUT:
            return "Demandé par le formulaire de ce broker, d'après le projet OptOut."
        case Source.ERASER if field is ProfileField.POSTAL_ADDRESS:
            return (
                "Estimation : ce type de site classe les personnes par adresse ; "
                f"{_ERASER_ESTIMATE}."
            )
        case Source.ERASER:
            return f"Estimation : nécessaire à toute demande par email ; {_ERASER_ESTIMATE}."
        case _:
            return "Pas encore de justification détaillée dans la fiche de ce broker."


def sent_items(broker: Broker, profile: Profile) -> list[str]:
    """What a request to this broker will contain, in words ("nom", "2 adresses email")."""
    counts = {
        ProfileField.FULL_NAME: 1,
        ProfileField.EMAIL: len(profile.emails_to_erase),
        ProfileField.POSTAL_ADDRESS: (1 if profile.postal_address else 0)
        + len(profile.previous_addresses),
        ProfileField.BIRTH_DATE: 1 if profile.birth_date else 0,
        ProfileField.PHONE: (1 if profile.phone else 0) + len(profile.other_phones),
    }
    words = {
        ProfileField.FULL_NAME: ("nom", "nom"),
        ProfileField.EMAIL: ("adresse email", "adresses email"),
        ProfileField.POSTAL_ADDRESS: ("adresse postale", "adresses postales"),
        ProfileField.BIRTH_DATE: ("date de naissance", "date de naissance"),
        ProfileField.PHONE: ("téléphone", "téléphones"),
    }
    items = []
    for kind in broker.required_fields:
        count = counts[kind]
        one, several = words[kind]
        items.append(one if count <= 1 else f"{count} {several}")
    return items
