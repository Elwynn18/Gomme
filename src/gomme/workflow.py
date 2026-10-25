"""What the commands do: queue requests, send them, follow them up.

Side effects stay at the edges: the tracker (database), the outbox files and the SMTP
sender are passed in, and so is the clock, so every step can be tested without a
network, a real mailbox or the real date.
"""

import contextlib
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from gomme.brokers.schema import Broker, EmailChannel, Region, WebFormChannel
from gomme.channels.email import (
    RecipientRefusedError,
    SendError,
    SmtpSender,
    build_message,
    mailto_link,
    write_eml,
)
from gomme.channels.postal import LetterError, compose_letter, postal_address, render_pdf
from gomme.channels.web_form import Page, Prompter, StepFailedError, run_steps
from gomme.compose import (
    EU_EEA,
    LABELS,
    RequestText,
    choose_jurisdiction,
    compose,
    compose_reminder,
)
from gomme.config import Config, EmailMode
from gomme.privacy import mask_email, write_private_bytes
from gomme.profile import MissingProfileDataError, Profile, placeholder_values
from gomme.tracker.db import RequestRecord, Tracker
from gomme.tracker.state import Action, Status

SUPPORTED_CHANNELS = ("email", "web_form", "postal")
# Journal notes of the messages that went through the user's SMTP server: they count
# toward the daily sending limit.
SMTP_SEND_NOTE = "email envoyé à"
SMTP_REMINDER_NOTE = "relance envoyée à"


@dataclass
class Report:
    done: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (broker slug, reason)
    errors: list[str] = field(default_factory=list)


def preferred_channel(broker: Broker, wanted: str | None = None) -> str | None:
    """The broker's first channel, or its first channel of type `wanted`."""
    for channel in broker.channels:
        if wanted is None or channel.type == wanted:
            return channel.type
    return None


def email_address(broker: Broker) -> str | None:
    for channel in broker.channels:
        if isinstance(channel, EmailChannel):
            return channel.address
    return None


# --- queue ---------------------------------------------------------------------------


def default_selection(brokers: Iterable[Broker], profile: Profile) -> tuple[str, list[Broker]]:
    """The brokers likely to hold data on the user, and a description of the choice.

    Writing to a broker that never had your data would hand it your name and address:
    by default, only brokers of the user's own market are selected, plus worldwide ones.
    """
    return selection_for_country(brokers, profile.country)


def selection_for_country(brokers: Iterable[Broker], country: str) -> tuple[str, list[Broker]]:
    """`default_selection` for a country of residence, before any profile exists."""
    if country == "FR":
        description = "fiches France (fr) et internationales (global)"
        chosen = [b for b in brokers if b.region in (Region.FR, Region.GLOBAL)]
    elif country in EU_EEA:
        description = f"fiches européennes de votre pays ({country}) et internationales"
        chosen = [
            b
            for b in brokers
            if b.region is Region.GLOBAL or (b.region is Region.EU and b.country == country)
        ]
    elif country == "US":
        description = "fiches américaines (us) et internationales (global)"
        chosen = [b for b in brokers if b.region in (Region.US, Region.GLOBAL)]
    else:
        description = "fiches internationales (global)"
        chosen = [b for b in brokers if b.region is Region.GLOBAL]
    return description, chosen


def queue_requests(
    tracker: Tracker,
    brokers: Iterable[Broker],
    config: Config,
    now: datetime,
    *,
    channel: str | None = None,
) -> Report:
    """Queue one request per broker, unless one is already under way."""
    report = Report()
    for broker in brokers:
        chosen = preferred_channel(broker, channel)
        if chosen is None:
            report.skipped.append((broker.slug, f"aucun canal {channel}"))
            continue
        missing = config.profile.missing(broker.required_fields)
        if missing:
            fields = ", ".join(field.value for field in missing)
            report.skipped.append((broker.slug, f"profil incomplet ({fields})"))
            continue
        latest = tracker.latest_for(broker.slug)
        jurisdiction = choose_jurisdiction(config.profile, broker)
        if latest is None:
            request = tracker.create(broker.slug, chosen, jurisdiction, now)
            tracker.apply(request.id, Action.QUEUE, now)
        elif latest.status in (Status.DRAFT, Status.RECHECK_DUE):
            # The broker's channels, or the user's country, may have changed since.
            tracker.retarget(latest.id, chosen, jurisdiction, now)
            tracker.apply(latest.id, Action.QUEUE, now)
        else:
            report.skipped.append(
                (broker.slug, f"demande n° {latest.id} déjà en cours ({latest.status})")
            )
            continue
        report.done.append(broker.slug)
    return report


# --- send ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Prepared:
    request: RequestRecord
    broker: Broker
    to: str
    text: RequestText


@dataclass
class SendReport(Report):
    previews: list[Prepared] = field(default_factory=list)
    files: list[tuple[int, Path, str]] = field(default_factory=list)  # (id, .eml, mailto)
    limit_reached: bool = False


def prepare_emails(
    tracker: Tracker, brokers: Mapping[str, Broker], config: Config, report: Report
) -> list[Prepared]:
    prepared = []
    for request in tracker.requests(status=Status.QUEUED):
        if request.channel != "email":
            continue
        broker = brokers.get(request.broker_slug)
        if broker is None:
            report.skipped.append((request.broker_slug, "fiche broker introuvable"))
            continue
        to = email_address(broker)
        if to is None:
            report.skipped.append((broker.slug, "plus d'adresse email dans la fiche"))
            continue
        try:
            text = compose(
                broker,
                config.profile,
                jurisdiction=request.jurisdiction,
                access=config.requests.access,
                notify_recipients=config.requests.notify_recipients,
            )
        except MissingProfileDataError as exc:
            report.skipped.append((broker.slug, str(exc)))
            continue
        prepared.append(Prepared(request, broker, to, text))
    return prepared


def send_emails(
    tracker: Tracker,
    brokers: Mapping[str, Broker],
    config: Config,
    clock: Callable[[], datetime],
    *,
    outbox: Path,
    dry_run: bool,
    limit: int | None = None,
    sender: Callable[[], SmtpSender] | None = None,
) -> SendReport:
    """Send queued email requests (SMTP mode) or write them as .eml files (manual mode).

    `dry_run` composes everything and writes or sends nothing. In SMTP mode, `sender`
    opens the SMTP session; it is only called when there is something to send.
    """
    report = SendReport()
    prepared = prepare_emails(tracker, brokers, config, report)
    today = clock().date()
    quota = config.email.daily_limit - tracker.notes_on(today, (SMTP_SEND_NOTE, SMTP_REMINDER_NOTE))
    if limit is not None:
        quota = min(quota, limit)
    if len(prepared) > max(quota, 0):
        report.limit_reached = True
        prepared = prepared[: max(quota, 0)]

    if dry_run:
        report.previews = prepared
        return report

    profile = config.profile
    if config.email.mode is EmailMode.MANUAL:
        for item in prepared:
            message = build_message(
                item.text,
                to=item.to,
                sender_name=profile.full_name,
                sender_address=profile.email,
                now=clock(),
            )
            path = write_eml(message, outbox / f"{item.request.id}-{item.broker.slug}.eml")
            report.files.append((item.request.id, path, mailto_link(item.text, item.to)))
            report.done.append(item.broker.slug)
        return report

    if not prepared:
        return report
    if sender is None:
        raise ValueError("mode smtp : session SMTP requise")
    bcc = [profile.email] if config.email.bcc_self else []
    try:
        with sender() as session:
            for item in prepared:
                now = clock()
                message = build_message(
                    item.text,
                    to=item.to,
                    sender_name=profile.full_name,
                    sender_address=profile.email,
                    now=now,
                )
                try:
                    session.send(message, bcc=bcc)
                except RecipientRefusedError as exc:
                    # This broker only: journal it and go on with the next request.
                    tracker.note(item.request.id, now, f"envoi impossible : {exc}")
                    report.errors.append(f"n° {item.request.id} ({item.broker.slug}) : {exc}")
                    continue
                # Sent: record it before anything else can fail, so it is never resent.
                proof: Path | None = None
                # The proof is a convenience; the journal is what matters.
                with contextlib.suppress(OSError):
                    proof = write_eml(
                        message, outbox / "sent" / f"{item.request.id}-{item.broker.slug}.eml"
                    )
                tracker.apply(
                    item.request.id,
                    Action.SEND,
                    now,
                    note=f"{SMTP_SEND_NOTE} {mask_email(item.to)}"
                    + ("" if proof else " (copie non enregistrée)"),
                    reference=str(message["Message-ID"]),
                    artifact=str(proof) if proof else None,
                )
                report.done.append(item.broker.slug)
    except SendError as exc:
        # The whole session is affected (connection, password, quota): stop here.
        report.errors.append(str(exc))
    return report


# --- monitor -------------------------------------------------------------------------


def completed_on(tracker: Tracker, request: RequestRecord) -> date | None:
    for event in reversed(tracker.events(request.id)):
        if event.to_status is Status.COMPLETED and event.from_status is not Status.COMPLETED:
            return event.at.date()
    return None


def monitor(
    tracker: Tracker, brokers: Mapping[str, Broker], config: Config, now: datetime
) -> Report:
    """Mark overdue requests, and completed ones due for a re-check. Safe to run often."""
    report = Report()
    today = now.date()
    for status in (Status.SENT, Status.ACKNOWLEDGED):
        for request in tracker.requests(status=status):
            deadline = request.deadline(brokers.get(request.broker_slug))
            if deadline is not None and deadline.is_overdue(today):
                tracker.apply(
                    request.id,
                    Action.MARK_OVERDUE,
                    now,
                    note=f"échéance du {deadline.due.isoformat()} dépassée",
                )
                report.done.append(f"{request.broker_slug} : en retard")
    recheck_after = timedelta(days=config.monitor.recheck_after_days)
    for request in tracker.requests(status=Status.COMPLETED):
        finished = completed_on(tracker, request)
        if finished is not None and today - finished >= recheck_after:
            tracker.apply(request.id, Action.RECHECK, now, note="re-vérification à faire")
            report.done.append(f"{request.broker_slug} : à re-vérifier")
    return report


# --- letters --------------------------------------------------------------------------------


@dataclass
class LettersReport(Report):
    files: list[tuple[int, Path]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def write_letters(
    tracker: Tracker,
    brokers: Mapping[str, Broker],
    config: Config,
    today: date,
    *,
    letters_dir: Path,
) -> LettersReport:
    """A PDF letter for each queued postal request. The user prints and posts it, then
    records the sending with `gomme mark <n> send`."""
    report = LettersReport()
    for request in tracker.requests(status=Status.QUEUED):
        if request.channel != "postal":
            continue
        broker = brokers.get(request.broker_slug)
        if broker is None:
            report.skipped.append((request.broker_slug, "fiche broker introuvable"))
            continue
        try:
            text = compose(
                broker,
                config.profile,
                jurisdiction=request.jurisdiction,
                access=config.requests.access,
                notify_recipients=config.requests.notify_recipients,
            )
            letter = compose_letter(broker, config.profile, text, today)
        except (MissingProfileDataError, LetterError) as exc:
            report.skipped.append((broker.slug, str(exc)))
            continue
        path = letters_dir / f"{request.id}-{broker.slug}.pdf"
        write_private_bytes(path, render_pdf(letter, title=text.subject))
        if letter.transliterated:
            report.warnings.append(
                f"n° {request.id} : imprimé sans accent : {' '.join(letter.transliterated)} "
                "(à corriger à la main sur le courrier si besoin)"
            )
        report.files.append((request.id, path))
        report.done.append(broker.slug)
    return report


# --- web forms -----------------------------------------------------------------------------


@dataclass
class SubmitReport(Report):
    proofs: list[tuple[int, Path]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def web_form_channel(broker: Broker) -> WebFormChannel | None:
    for channel in broker.channels:
        if isinstance(channel, WebFormChannel):
            return channel
    return None


def submit_web_forms(
    tracker: Tracker,
    brokers: Mapping[str, Broker],
    config: Config,
    clock: Callable[[], datetime],
    *,
    open_page: Callable[[], AbstractContextManager[Page]],
    prompter: Prompter,
    confirm: Callable[[str], bool],
    open_url: Callable[[str], object],
    proofs_dir: Path,
    only: str | None = None,
) -> SubmitReport:
    """Fill in the queued web form requests, one at a time, with the user watching.

    A request is recorded as sent only once the user confirms it went through.
    """
    report = SubmitReport()
    language = config.profile.language
    for request in tracker.requests(status=Status.QUEUED):
        if request.channel != "web_form" or (only and request.broker_slug != only):
            continue
        broker = brokers.get(request.broker_slug)
        channel = web_form_channel(broker) if broker else None
        if broker is None or channel is None:
            report.skipped.append((request.broker_slug, "fiche ou formulaire introuvable"))
            continue
        try:
            values = placeholder_values(config.profile, broker.required_fields, clock().date())
        except MissingProfileDataError as exc:
            report.skipped.append((broker.slug, str(exc)))
            continue

        prompter.show(f"n° {request.id} — {broker.name}")
        if not channel.steps:
            fields = ", ".join(LABELS[language][field] for field in broker.required_fields)
            open_url(str(channel.url))
            prompter.pause(
                f"Le formulaire de {broker.name} est ouvert dans votre navigateur. "
                f"Remplissez-le avec ces informations seulement : {fields}."
                if language == "fr"
                else f"{broker.name}'s form is open in your browser. Fill it in with this "
                f"information only: {fields}."
            )
            note = "formulaire rempli à la main"
            proof = None
        else:
            try:
                with open_page() as page:
                    result = run_steps(
                        channel,
                        page,
                        prompter,
                        values,
                        language=language,
                        proof_path=proofs_dir / f"{request.id}-{broker.slug}.png",
                    )
            except StepFailedError as exc:
                report.errors.append(f"{broker.slug} : {exc}")
                tracker.note(request.id, clock(), f"échec du formulaire : {exc}")
                continue
            report.warnings += [f"{broker.slug} : {warning}" for warning in result.warnings]
            proof = result.screenshots[-1] if result.screenshots else None
            note = "formulaire rempli par Gomme"
        if not confirm(
            "La demande a-t-elle bien été envoyée ?" if language == "fr" else "Was it submitted?"
        ):
            report.skipped.append((broker.slug, "envoi non confirmé"))
            continue
        tracker.apply(
            request.id, Action.SEND, clock(), note=note, artifact=str(proof) if proof else None
        )
        if proof:
            report.proofs.append((request.id, proof))
        report.done.append(broker.slug)
    return report


# --- reminders ------------------------------------------------------------------------------


class ReminderError(ValueError):
    pass


@dataclass
class ReminderReport:
    text: RequestText
    to: str | None  # email address, or None for a letter
    path: Path | None = None
    sent: bool = False


def send_reminder(
    tracker: Tracker,
    brokers: Mapping[str, Broker],
    config: Config,
    clock: Callable[[], datetime],
    request_id: int,
    *,
    outbox: Path,
    dry_run: bool,
    sender: Callable[[], SmtpSender] | None = None,
) -> ReminderReport:
    """Remind a broker of an overdue request, by email when it has an address, else by
    letter. The status stays `overdue`; the reminder is journaled."""
    request = tracker.get(request_id)
    if request.status is not Status.OVERDUE:
        raise ReminderError(
            f"seule une demande en retard se relance (état : {request.status}) ; "
            "lancer d'abord gomme monitor"
        )
    broker = brokers.get(request.broker_slug)
    if broker is None:
        raise ReminderError(f"fiche broker introuvable : {request.broker_slug}")
    deadline = request.deadline(broker)
    if deadline is None or request.sent_on is None:  # pragma: no cover - overdue implies sent
        raise ReminderError("demande jamais envoyée")
    events = tracker.events(request_id)
    text = compose_reminder(
        broker,
        config.profile,
        request.jurisdiction,
        sent_on=request.sent_on,
        due=deadline.due,
        # An announced extension is an answer too.
        acknowledged=request.extended
        or any(event.action == Action.ACKNOWLEDGE.value for event in events),
        extended=request.extended,
    )
    number = 1 + sum(1 for event in events if event.note.startswith("relance"))
    stem = f"{request.id}-{broker.slug}-relance-{number}"
    to = email_address(broker)
    if to is None and postal_address(broker) is None:
        form = web_form_channel(broker)
        where = f" : {form.url}" if form else ""
        raise ReminderError(
            f"{broker.slug} n'a ni email ni adresse postale : relancer par son formulaire{where}"
        )
    report = ReminderReport(text, to)
    if dry_run:
        return report

    now = clock()
    if to is None:
        try:
            letter = compose_letter(broker, config.profile, text, now.date())
        except MissingProfileDataError:
            raise ReminderError(
                "relance par courrier : renseigner votre adresse postale (gomme init --force)"
            ) from None
        except LetterError as exc:
            raise ReminderError(f"relance par courrier impossible : {exc}") from None
        report.path = outbox.parent / "letters" / f"{stem}.pdf"
        write_private_bytes(report.path, render_pdf(letter, title=text.subject))
        tracker.note(request_id, now, f"relance préparée par courrier : {report.path}")
        return report

    profile = config.profile
    message = build_message(
        text,
        to=to,
        sender_name=profile.full_name,
        sender_address=profile.email,
        now=now,
        in_reply_to=request.reference if request.channel == "email" else None,
    )
    if config.email.mode is EmailMode.MANUAL:
        report.path = write_eml(message, outbox / f"{stem}.eml")
        tracker.note(request_id, now, f"relance préparée : {report.path}")
        return report
    if sender is None:
        raise ValueError("mode smtp : session SMTP requise")
    with sender() as session:
        session.send(message, bcc=[profile.email] if config.email.bcc_self else [])
    # Sent: journal it before anything else can fail (numbering, daily limit).
    report.sent = True
    tracker.note(request_id, now, f"{SMTP_REMINDER_NOTE} {mask_email(to)}")
    with contextlib.suppress(OSError):
        report.path = write_eml(message, outbox / "sent" / f"{stem}.eml")
    return report
