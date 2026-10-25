"""Draft of a complaint to the CNIL (GDPR art. 77) for a request left unanswered or refused.

Gomme only prepares the text: the user reviews it, completes it and files it at
https://www.cnil.fr/fr/adresser-une-plainte. The CNIL expects the rights to have been
exercised with the organisation first, one month without answer (or a refusal contested
with the organisation), a complaint written in French, and copies of the exchanges
(https://www.cnil.fr/fr/adresser-une-plainte, checked 2026-10-25).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from gomme.brokers.schema import Broker, EmailChannel, PostalChannel, WebFormChannel
from gomme.compose import template_environment
from gomme.legal.statutes import Jurisdiction
from gomme.profile import Profile
from gomme.tracker.db import EventRecord, RequestRecord
from gomme.tracker.state import Action, Status

CNIL_COMPLAINT_URL = "https://www.cnil.fr/fr/adresser-une-plainte"

CHANNEL_LABELS = {
    "email": "par email",
    "web_form": "par son formulaire en ligne",
    "postal": "par courrier",
}
EVENT_LABELS = {
    "create": "demande préparée",
    Action.SEND.value: "demande envoyée",
    Action.ACKNOWLEDGE.value: "accusé de réception de l'organisme",
    Action.EXTEND.value: "l'organisme annonce une prolongation du délai",
    Action.REQUEST_ACTION.value: "l'organisme demande une information complémentaire",
    Action.ACTION_DONE.value: "information complémentaire fournie",
    Action.REFUSE.value: "refus de l'organisme",
    Action.MARK_OVERDUE.value: "délai légal de réponse dépassé",
    "note": "note",
}


class ComplaintError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    date: str
    label: str
    note: str


def _french_date(day: date) -> str:
    return day.strftime("%d/%m/%Y")


def contact_lines(broker: Broker) -> list[str]:
    lines = []
    for channel in broker.channels:
        if isinstance(channel, EmailChannel):
            lines.append(f"Adresse email de contact : {channel.address}")
        elif isinstance(channel, WebFormChannel):
            lines.append(f"Formulaire : {channel.url}")
        elif isinstance(channel, PostalChannel):
            lines.append(f"Adresse postale : {channel.address}")
    return lines


def timeline(events: Sequence[EventRecord]) -> list[TimelineEntry]:
    return [
        TimelineEntry(_french_date(event.at.date()), EVENT_LABELS[event.action], event.note)
        for event in events
        if event.action in EVENT_LABELS and event.action not in ("queue", "unqueue")
    ]


def compose_complaint(
    broker: Broker,
    profile: Profile,
    request: RequestRecord,
    events: Sequence[EventRecord],
    today: date,
) -> str:
    if request.jurisdiction is not Jurisdiction.GDPR:
        raise ComplaintError(
            "seules les demandes RGPD peuvent donner lieu à une plainte auprès de la CNIL"
        )
    if request.status not in (Status.OVERDUE, Status.REFUSED) or request.sent_on is None:
        raise ComplaintError(
            "une plainte se prépare pour une demande en retard ou refusée "
            f"(état actuel : {request.status})"
        )
    deadline = request.deadline(broker)
    assert deadline is not None  # noqa: S101 - the request was sent
    refused = request.status is Status.REFUSED
    refusal = next((e for e in reversed(events) if e.action == Action.REFUSE.value), None)
    subject = (
        f"Refus de {broker.name} de faire droit à une demande d'effacement et d'opposition"
        if refused
        else f"Absence de réponse de {broker.name} à une demande d'effacement et d'opposition"
    )
    return (
        template_environment()
        .get_template("fr/complaint_cnil.txt.j2")
        .render(
            subject=subject,
            broker=broker,
            profile=profile,
            contacts=contact_lines(broker),
            sent_on=_french_date(request.sent_on),
            channel_label=CHANNEL_LABELS[request.channel],
            refused=refused,
            refused_on=_french_date(refusal.at.date()) if refusal else None,
            extended=request.extended,
            due=_french_date(deadline.due),
            today=_french_date(today),
            acknowledged=any(e.action == Action.ACKNOWLEDGE.value for e in events),
            events=timeline(events),
            artifact=request.artifact,
        )
    )
