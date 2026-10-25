"""Command-line entry point. Thin layer: the work is done in gomme.workflow."""

import threading
import time
import webbrowser
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import date, datetime
from enum import StrEnum
from importlib.metadata import version
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from gomme.brokers.registry import RegistryError, default_brokers_dir, load_registry
from gomme.brokers.schema import Broker, EmailChannel, Region, WebFormChannel
from gomme.channels.browser import BrowserUnavailableError, open_browser
from gomme.channels.email import SendError, SmtpSender
from gomme.channels.web_form import Page, verify_form
from gomme.config import (
    Config,
    ConfigError,
    EmailConfig,
    EmailMode,
    SmtpConfig,
    SmtpSecurity,
    config_is_private,
    config_path,
    data_dir,
    load_config,
    save_config,
    smtp_password,
)
from gomme.labels import MANUAL_ACTIONS, STATUS_LABELS
from gomme.legal.complaint import CNIL_COMPLAINT_URL, ComplaintError, compose_complaint
from gomme.privacy import mask_email, write_private_file
from gomme.profile import Profile
from gomme.tracker.db import Tracker, TrackerError, default_db_path
from gomme.tracker.state import Action, InvalidTransitionError, Status
from gomme.workflow import (
    ReminderError,
    Report,
    default_selection,
    monitor,
    queue_requests,
    send_emails,
    send_reminder,
    submit_web_forms,
    web_form_channel,
    write_letters,
)

BROKERS_DIR_ENV = "GOMME_BROKERS_DIR"
# Delete Act (SB 362): consumers can submit requests since 1 January 2026, registered
# brokers must process them since 1 August 2026 (privacy.ca.gov, checked 2026-10-25).
CALIFORNIA_DROP_URL = "https://privacy.ca.gov/drop/"

app = typer.Typer(no_args_is_help=True)
brokers_app = typer.Typer(no_args_is_help=True, help="Fiches des courtiers en données.")
app.add_typer(brokers_app, name="brokers")

out = Console()
err = Console(stderr=True)

BrokersDir = Annotated[
    Path | None,
    typer.Option(
        envvar=BROKERS_DIR_ENV,
        help="Dossier des fiches (par défaut : les fiches fournies avec Gomme).",
        show_default=False,
    ),
]


def now() -> datetime:
    """Local time, with its offset. A function so tests can replace the clock."""
    return datetime.now().astimezone()


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"gomme {version('gomme')}")
        raise typer.Exit


@app.callback()
def main(
    show_version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_print_version, is_eager=True, help="Afficher la version."
        ),
    ] = False,
) -> None:
    """Demandes de suppression de données personnelles auprès des courtiers en données."""


# --- Shared helpers ----------------------------------------------------------------------


def _fail(message: str) -> typer.Exit:
    err.print(f"[bold red]Erreur :[/] {message}", highlight=False)
    return typer.Exit(code=1)


def _registry(brokers_dir: Path | None) -> tuple[Broker, ...]:
    try:
        return load_registry(brokers_dir if brokers_dir is not None else default_brokers_dir())
    except RegistryError as exc:
        err.print(f"[bold red]Fiches broker invalides[/] ({len(exc.errors)} problème(s)) :")
        for error in exc.errors:
            err.print(f"  • {error}", markup=False, highlight=False, soft_wrap=True)
        raise typer.Exit(code=1) from None


def _config() -> Config:
    try:
        config = load_config()
    except ConfigError as exc:
        raise _fail(str(exc)) from None
    if not config_is_private():
        err.print(
            f"[yellow]Attention :[/] {config_path()} est lisible par d'autres utilisateurs. "
            f"Corriger avec : chmod 600 {config_path()}",
            highlight=False,
        )
    return config


@contextmanager
def _tracker() -> Iterator[Tracker]:
    with Tracker(default_db_path(data_dir())) as tracker:
        yield tracker


def _print_report(report: Report, done_label: str) -> None:
    out.print(f"{len(report.done)} {done_label}")
    reasons = Counter(reason for _, reason in report.skipped)
    if report.skipped:
        out.print(f"{len(report.skipped)} écartée(s) :")
        for reason, count in reasons.most_common():
            out.print(f"  • {reason} : {count}", highlight=False)
    for error in report.errors:
        err.print(f"[bold red]Erreur :[/] {error}", highlight=False)


# --- brokers ---------------------------------------------------------------------------


@brokers_app.command("list")
def list_brokers(
    region: Annotated[Region | None, typer.Option(help="Limiter à une région.")] = None,
    channel: Annotated[
        str | None, typer.Option(help="Limiter à un canal : email, web_form ou postal.")
    ] = None,
    brokers_dir: BrokersDir = None,
) -> None:
    """Lister les fiches broker."""
    brokers = _registry(brokers_dir)
    if region is not None:
        brokers = tuple(broker for broker in brokers if broker.region is region)
    if channel is not None:
        brokers = tuple(b for b in brokers if any(c.type == channel for c in b.channels))

    if not brokers:
        where = "" if region is None else f" pour la région « {region} »"
        out.print(f"Aucune fiche broker{where}.")
        return
    out.print(_brokers_table(brokers))


@brokers_app.command("show")
def show_broker(
    slug: Annotated[str, typer.Argument(help="Slug du broker.")],
    brokers_dir: BrokersDir = None,
) -> None:
    """Afficher une fiche : canaux, champs envoyés, provenance, notes."""
    broker = {b.slug: b for b in _registry(brokers_dir)}.get(slug)
    if broker is None:
        raise _fail(f"broker inconnu : {slug}")
    table = Table(show_header=False, title=Text(broker.name))
    rows: list[tuple[str, str]] = [
        ("slug", broker.slug),
        ("domaine", broker.domain),
        ("pays / région", f"{broker.country or '—'} / {broker.region}"),
        ("catégorie", broker.category),
    ]
    for channel in broker.channels:
        if isinstance(channel, WebFormChannel):
            mode = f"{len(channel.steps)} étape(s) automatisée(s)" if channel.steps else "manuel"
            rows.append(("formulaire", f"{channel.url} ({mode})"))
        elif isinstance(channel, EmailChannel):
            rows.append(("email", channel.address))
        else:
            rows.append(("courrier", channel.address))
    rows += [
        ("bases légales", ", ".join(broker.legal_basis)),
        ("champs envoyés", ", ".join(broker.required_fields)),
        ("pièce d'identité", "exigée (à caviarder)" if broker.requires_id else "non"),
        ("source", f"{broker.source} : {broker.source_ref}"),
        ("vérifiée le", broker.last_verified.isoformat() if broker.last_verified else "—"),
    ]
    for label, value in rows:
        table.add_row(label, Text(value))
    out.print(table)
    if broker.notes:
        out.print(Text(broker.notes), style="dim")


def _brokers_table(brokers: Sequence[Broker]) -> Table:
    table = Table(caption=f"{len(brokers)} fiche(s)")
    # The slug (the identifier used by other commands) and the date are never cut.
    for header in ("Slug", "Nom", "Région", "Pays", "Catégorie", "Canaux", "Vérifiée"):
        table.add_column(header, no_wrap=header in {"Slug", "Vérifiée"})
    for broker in brokers:
        verified = broker.last_verified.isoformat() if broker.last_verified else "—"
        # Text() keeps values from being interpreted as Rich markup.
        table.add_row(
            Text(broker.slug),
            Text(broker.name),
            Text(broker.region),
            Text(broker.country or "—"),
            Text(broker.category),
            Text(", ".join(channel.type for channel in broker.channels)),
            Text(verified),
        )
    return table


# --- init / profile -------------------------------------------------------------------


def _ask(label: str, default: str | None = None, *, optional: bool = False) -> str | None:
    value: str = typer.prompt(
        label + (" (laisser vide pour ignorer)" if optional else ""),
        default="" if optional and default is None else default,
        show_default=bool(default),
    )
    value = value.strip()
    return value or None


def _describe_errors(exc: ValidationError) -> str:
    return "\n".join(
        f"  • {'.'.join(map(str, error['loc']))} : {error['msg']}"
        for error in exc.errors(include_url=False, include_input=False)
    )


def _ask_choice[E: StrEnum](label: str, default: str, choices: type[E]) -> E:
    while True:
        value = _ask_required(label, default).lower()
        try:
            return choices(value)
        except ValueError:
            err.print("Valeurs possibles : " + ", ".join(choice.value for choice in choices))


def _ask_port(default: str) -> int:
    while True:
        value = _ask_required("  Port", default)
        if value.isdigit() and 0 < int(value) < 65536:
            return int(value)
        err.print("Un numéro de port entre 1 et 65535.")


def _ask_required(label: str, default: str | None = None) -> str:
    while True:
        value = _ask(label, default)
        if value:
            return value
        err.print("Ce champ est obligatoire.")


@app.command()
def init(
    force: Annotated[bool, typer.Option(help="Remplacer la configuration existante.")] = False,
) -> None:
    """Créer la configuration : votre profil et la façon d'envoyer les emails."""
    path = config_path()
    if path.exists() and not force:
        raise _fail(f"{path} existe déjà (relancer avec --force pour la remplacer)")

    out.print(
        "Gomme envoie des demandes [bold]en votre nom uniquement[/]. Vos données restent sur "
        "cette machine ; chaque broker ne reçoit que les champs dont il a besoin."
    )
    profile_data: dict[str, object] = {
        "first_name": _ask_required("Prénom"),
        "last_name": _ask_required("Nom"),
        "email": _ask_required("Adresse email (celle qui enverra les demandes)"),
        "country": _ask_required("Pays de résidence (code ISO, ex. FR)", "FR").upper(),
        "language": _ask_required("Langue de l'interface (fr ou en)", "fr"),
    }
    out.print("Certains brokers demandent votre adresse pour vous retrouver.")
    if typer.confirm("Renseigner une adresse postale ?", default=True):
        country = str(profile_data["country"])
        address: dict[str, object] = {
            "street": _ask_required("  Rue"),
            "postal_code": _ask_required("  Code postal"),
            "city": _ask_required("  Ville"),
            "country": country,
        }
        if country == "US":
            address["state"] = _ask_required("  État (code, ex. CA)").upper()
        profile_data["postal_address"] = address
    birth = _ask("Date de naissance (AAAA-MM-JJ), demandée par quelques brokers", optional=True)
    if birth:
        profile_data["birth_date"] = birth
    phone = _ask("Téléphone, demandé par quelques brokers", optional=True)
    if phone:
        profile_data["phone"] = phone

    email_config = EmailConfig()
    if typer.confirm(
        "Envoyer les emails via votre serveur SMTP ? (sinon : fichiers .eml à envoyer vous-même)",
        default=False,
    ):
        security = _ask_choice("  Sécurité (starttls ou ssl)", "starttls", SmtpSecurity)
        default_port = "587" if security is SmtpSecurity.STARTTLS else "465"
        host = _ask_required("  Serveur SMTP")
        port = _ask_port(default_port)
        username = _ask_required("  Identifiant", str(profile_data["email"]))
        try:
            smtp = SmtpConfig(host=host, port=port, security=security, username=username)
        except ValidationError as exc:
            raise _fail(f"réglages SMTP invalides :\n{_describe_errors(exc)}") from None
        email_config = EmailConfig(mode=EmailMode.SMTP, smtp=smtp)
    try:
        config = Config(profile=Profile.model_validate(profile_data), email=email_config)
    except ValidationError as exc:
        # Field names only: the values typed are personal data.
        raise _fail(f"profil invalide :\n{_describe_errors(exc)}") from None
    save_config(config, path)
    out.print(f"Configuration enregistrée dans {path} (lisible par vous seul).", highlight=False)
    if config.email.smtp is not None:
        out.print(
            f"Le mot de passe SMTP n'y est pas : le mettre dans la variable "
            f"{config.email.smtp.password_env} au moment d'envoyer.",
            highlight=False,
        )


@app.command()
def profile() -> None:
    """Afficher votre profil (masqué) et l'emplacement de la configuration."""
    config = _config()
    table = Table(show_header=False)
    for label, value in config.profile.masked().items():
        table.add_row(label, Text(value))
    table.add_row("envoi", config.email.mode.value)
    out.print(table)
    out.print(f"Configuration : {config_path()}", highlight=False, soft_wrap=True)


# --- queue / send -----------------------------------------------------------------------


@app.command()
def queue(
    broker: Annotated[
        list[str] | None, typer.Option("--broker", help="Slug d'un broker (répétable).")
    ] = None,
    region: Annotated[
        list[Region] | None,
        typer.Option(help="Région (répétable). Par défaut : celles de votre pays."),
    ] = None,
    channel: Annotated[
        str | None, typer.Option(help="N'utiliser que ce canal : email, web_form, postal.")
    ] = None,
    brokers_dir: BrokersDir = None,
) -> None:
    """Préparer une demande pour chaque broker qui n'en a pas déjà une en cours."""
    config = _config()
    brokers = _registry(brokers_dir)
    if broker:
        known = {b.slug: b for b in brokers}
        unknown = [slug for slug in broker if slug not in known]
        if unknown:
            raise _fail(f"broker(s) inconnu(s) : {', '.join(unknown)}")
        selected = tuple(known[slug] for slug in broker)
    elif region:
        selected = tuple(b for b in brokers if b.region in region)
        out.print(f"Régions : {', '.join(region)}", highlight=False)
    else:
        description, chosen = default_selection(brokers, config.profile)
        selected = tuple(chosen)
        out.print(
            f"Sélection : {description}. Autres régions : --region eu, --region us...",
            highlight=False,
        )
    with _tracker() as tracker:
        report = queue_requests(tracker, selected, config, now(), channel=channel)
    _print_report(report, "demande(s) mise(s) en file")
    address = config.profile.postal_address
    if address is not None and address.country == "US" and address.state == "CA":
        out.print(
            "Résident de Californie : la plateforme officielle DROP envoie une seule demande "
            f"à tous les brokers enregistrés dans l'État : {CALIFORNIA_DROP_URL}",
            highlight=False,
        )


@app.command()
def send(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Tout préparer, ne rien envoyer ni écrire.")
    ] = False,
    limit: Annotated[int | None, typer.Option(min=1, help="Nombre maximal d'emails.")] = None,
    show: Annotated[bool, typer.Option(help="Afficher le texte complet des demandes.")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Ne pas demander confirmation.")] = False,
    brokers_dir: BrokersDir = None,
) -> None:
    """Envoyer les demandes par email en attente (ou écrire les fichiers .eml)."""
    config = _config()
    brokers = {b.slug: b for b in _registry(brokers_dir)}
    outbox = data_dir() / "outbox"
    smtp_mode = config.email.mode is EmailMode.SMTP and not dry_run

    sender: Callable[[], SmtpSender] | None = None
    if smtp_mode:
        smtp = config.email.smtp
        assert smtp is not None  # noqa: S101 - guaranteed by EmailConfig
        try:
            password = smtp_password(smtp)
        except ConfigError as exc:
            raise _fail(str(exc)) from None

        def sender() -> SmtpSender:
            return SmtpSender(smtp, password, delay_seconds=config.email.delay_seconds)

    with _tracker() as tracker:
        if smtp_mode and not yes:
            pending = sum(1 for r in tracker.requests(status=Status.QUEUED) if r.channel == "email")
            if pending and not typer.confirm(
                f"Envoyer jusqu'à {min(pending, limit or pending)} email(s) depuis "
                f"{mask_email(config.profile.email)} ?"
            ):
                raise typer.Exit(code=1)
        report = send_emails(
            tracker,
            brokers,
            config,
            now,
            outbox=outbox,
            dry_run=dry_run,
            limit=limit,
            sender=sender,
        )

    if dry_run:
        out.print(f"[bold]Simulation[/] : {len(report.previews)} email(s), rien n'est envoyé.")
        for item in report.previews:
            out.print(
                f"  n° {item.request.id} {item.broker.slug} → {item.to} : {item.text.subject}",
                highlight=False,
                markup=False,
            )
            if show:
                out.print(Text(item.text.body), style="dim")
    elif config.email.mode is EmailMode.MANUAL:
        for request_id, path, _mailto in report.files:
            out.print(f"  n° {request_id} : {path}", highlight=False)
        if report.files:
            out.print(
                "Ouvrez chaque fichier .eml dans votre logiciel de messagerie et envoyez-le, "
                "puis notez l'envoi : gomme mark <n°> send"
            )
    if dry_run:
        report.done = [item.broker.slug for item in report.previews]
    _print_report(report, "email(s) simulé(s)" if dry_run else "email(s) traité(s)")
    if report.limit_reached:
        out.print(
            f"Limite atteinte ({config.email.daily_limit} par jour) : relancer demain "
            "pour la suite."
        )
    if report.errors:
        raise typer.Exit(code=1)


# --- status / mark / history / monitor ----------------------------------------------------


def _deadline_cell(request_status: Status, due: date | None) -> Text:
    if due is None:
        return Text("—")
    days = (due - now().date()).days
    style = "red" if days < 0 and request_status in (Status.SENT, Status.ACKNOWLEDGED) else ""
    return Text(f"{due.isoformat()} ({days:+d} j)", style=style)


@app.command()
def status(
    show_all: Annotated[
        bool, typer.Option("--all", help="Inclure les demandes terminées.")
    ] = False,
    brokers_dir: BrokersDir = None,
) -> None:
    """Afficher les demandes, leur état et leurs échéances légales."""
    brokers = {b.slug: b for b in _registry(brokers_dir)}
    with _tracker() as tracker:
        requests = tracker.requests()
    if not show_all:
        requests = tuple(r for r in requests if r.status is not Status.COMPLETED)
    if not requests:
        out.print("Aucune demande. Commencer avec : gomme queue")
        return
    table = Table(caption=f"{len(requests)} demande(s)")
    for header in ("N°", "Broker", "Canal", "Loi", "État", "Envoyée", "Échéance"):
        table.add_column(header, no_wrap=header in {"N°", "Échéance"})
    for request in requests:
        broker = brokers.get(request.broker_slug)
        deadline = request.deadline(broker)
        table.add_row(
            str(request.id),
            Text(request.broker_slug),
            request.channel,
            request.jurisdiction.value.upper(),
            STATUS_LABELS[request.status],
            request.sent_on.isoformat() if request.sent_on else "—",
            _deadline_cell(request.status, deadline.due if deadline else None),
        )
    out.print(table)
    counts = Counter(r.status for r in requests)
    out.print(", ".join(f"{STATUS_LABELS[s]} : {n}" for s, n in counts.most_common()))


def parse_request_numbers(text: str) -> list[int]:
    """``3``, ``1,4`` or ``2-5,9`` -> request numbers, in order, without duplicates."""
    numbers: list[int] = []
    for part in text.replace(" ", "").split(","):
        first, dash, last = part.partition("-")
        if not first.isdigit() or (dash and not last.isdigit()):
            raise ValueError(f"numéro de demande invalide : {part!r}")
        start, end = int(first), int(last) if dash else int(first)
        if end < start or end - start > 1000:
            raise ValueError(f"plage invalide : {part!r}")
        numbers += [n for n in range(start, end + 1) if n not in numbers]
    return numbers


@app.command()
def mark(
    request_ids: Annotated[
        str, typer.Argument(help="Numéro(s) de demande : 3, 1,4 ou 2-5.", metavar="N")
    ],
    action: Annotated[
        str,
        typer.Argument(help="Action : " + ", ".join(a.value for a in MANUAL_ACTIONS)),
    ],
    note: Annotated[str, typer.Option(help="Note à joindre au journal.")] = "",
    sent_date: Annotated[
        datetime | None,
        typer.Option(
            "--date",
            formats=["%Y-%m-%d"],
            help="Pour send : date réelle de l'envoi (AAAA-MM-JJ), si ce n'est pas aujourd'hui.",
        ),
    ] = None,
    received_date: Annotated[
        datetime | None,
        typer.Option(
            "--received",
            formats=["%Y-%m-%d"],
            help="Pour send : date de réception par le broker (accusé de réception d'un "
            "courrier). Le délai légal court à partir de cette date.",
        ),
    ] = None,
) -> None:
    """Noter une étape : envoi manuel, réponse du broker, suppression confirmée..."""
    try:
        chosen = Action(action)
    except ValueError:
        chosen = None
    if chosen not in MANUAL_ACTIONS:
        raise _fail(
            f"action inconnue : {action} (possibles : "
            + ", ".join(a.value for a in MANUAL_ACTIONS)
            + ")"
        )
    assert chosen is not None  # noqa: S101 - checked just above
    try:
        numbers = parse_request_numbers(request_ids)
    except ValueError as exc:
        raise _fail(str(exc)) from None
    failed = 0
    with _tracker() as tracker:
        for request_id in numbers:
            try:
                record = tracker.apply(
                    request_id,
                    chosen,
                    now(),
                    note=note,
                    sent_on=sent_date.date() if sent_date else None,
                    received_on=received_date.date() if received_date else None,
                )
            except (InvalidTransitionError, TrackerError) as exc:
                failed += 1
                err.print(f"[bold red]Erreur :[/] n° {request_id} : {exc}", highlight=False)
                continue
            out.print(f"Demande n° {record.id} : {STATUS_LABELS[record.status]}", highlight=False)
    if failed:
        raise typer.Exit(code=1)


@app.command()
def received(
    request_id: Annotated[int, typer.Argument(help="Numéro de la demande.")],
    day: Annotated[
        datetime,
        typer.Argument(
            formats=["%Y-%m-%d"], help="Date de réception (AAAA-MM-JJ).", metavar="DATE"
        ),
    ],
    brokers_dir: BrokersDir = None,
) -> None:
    """Noter la date de réception d'un courrier (accusé de réception) : le délai en part."""
    brokers = {b.slug: b for b in _registry(brokers_dir)}
    with _tracker() as tracker:
        try:
            current = tracker.get(request_id)
            record = tracker.record_receipt(
                request_id, day.date(), now(), brokers.get(current.broker_slug)
            )
        except TrackerError as exc:
            raise _fail(str(exc)) from None
    deadline = record.deadline(brokers.get(record.broker_slug))
    due = f", échéance le {deadline.due.isoformat()}" if deadline else ""
    out.print(
        f"Demande n° {record.id} : reçue le {day.date().isoformat()}{due} "
        f"({STATUS_LABELS[record.status]})",
        highlight=False,
    )


@app.command()
def history(request_id: Annotated[int, typer.Argument(help="Numéro de la demande.")]) -> None:
    """Afficher le journal d'une demande."""
    with _tracker() as tracker:
        try:
            request = tracker.get(request_id)
        except TrackerError as exc:
            raise _fail(str(exc)) from None
        events = tracker.events(request_id)
    out.print(
        f"Demande n° {request.id} — {request.broker_slug} ({request.channel})", highlight=False
    )
    for event in events:
        line = f"  {event.at:%Y-%m-%d %H:%M}  {event.action:<15} → {STATUS_LABELS[event.to_status]}"
        out.print(line + (f"  — {event.note}" if event.note else ""), highlight=False, markup=False)


@app.command("monitor")
def monitor_command(brokers_dir: BrokersDir = None) -> None:
    """Marquer les demandes en retard et celles à re-vérifier (à lancer par cron)."""
    config = _config()
    brokers = {b.slug: b for b in _registry(brokers_dir)}
    with _tracker() as tracker:
        report = monitor(tracker, brokers, config, now())
    for line in report.done:
        out.print(f"  • {line}", highlight=False)
    out.print(f"{len(report.done)} changement(s)")


@app.command()
def escalate(
    request_id: Annotated[int, typer.Argument(help="Numéro de la demande.")],
    yes: Annotated[bool, typer.Option("--yes", help="Ne pas demander confirmation.")] = False,
    brokers_dir: BrokersDir = None,
) -> None:
    """Préparer un brouillon de plainte à la CNIL pour une demande en retard ou refusée."""
    config = _config()
    brokers = {b.slug: b for b in _registry(brokers_dir)}
    with _tracker() as tracker:
        try:
            request = tracker.get(request_id)
        except TrackerError as exc:
            raise _fail(str(exc)) from None
        broker = brokers.get(request.broker_slug)
        if broker is None:
            raise _fail(f"fiche broker introuvable : {request.broker_slug}")
        if (
            request.status is Status.REFUSED
            and not yes
            and not typer.confirm(
                "Avez-vous contesté ce refus auprès de l'organisme ? La CNIL demande de le "
                "faire avant de porter plainte."
            )
        ):
            raise typer.Exit(code=1)
        try:
            text = compose_complaint(
                broker, config.profile, request, tracker.events(request_id), now().date()
            )
        except ComplaintError as exc:
            raise _fail(str(exc)) from None
        path = data_dir() / "complaints" / f"{request.id}-{broker.slug}.txt"
        write_private_file(path, text)
        tracker.apply(request_id, Action.ESCALATE, now(), note=f"brouillon de plainte : {path}")
    out.print(f"Brouillon de plainte : {path}", highlight=False, soft_wrap=True)
    out.print(
        "Relisez-le, complétez les passages entre crochets (numéro SIREN, pièces), puis "
        f"déposez-le sur {CNIL_COMPLAINT_URL}. Gomme ne l'envoie pas.",
        highlight=False,
    )


@app.command()
def letters(brokers_dir: BrokersDir = None) -> None:
    """Préparer en PDF les courriers des demandes postales en attente."""
    config = _config()
    brokers = {b.slug: b for b in _registry(brokers_dir)}
    with _tracker() as tracker:
        report = write_letters(
            tracker, brokers, config, now().date(), letters_dir=data_dir() / "letters"
        )
    for request_id, path in report.files:
        out.print(f"  n° {request_id} : {path}", highlight=False, soft_wrap=True)
    if report.files:
        out.print(
            "Imprimez, signez et envoyez chaque courrier, de préférence en recommandé avec "
            "accusé de réception (gardez la preuve de dépôt). Notez l'envoi : gomme mark <n°> "
            "send ; puis, à l'arrivée de l'accusé, sa date : gomme received <n°> AAAA-MM-JJ "
            "(le délai légal court à partir de la réception)."
        )
    for warning in report.warnings:
        out.print(f"[yellow]Attention :[/] {warning}", highlight=False)
    _print_report(report, "courrier(s) préparé(s)")


# --- web forms ---------------------------------------------------------------------------------


class ConsolePrompter:
    """The engine's `Prompter`, in the terminal."""

    def show(self, message: str) -> None:
        out.print(message, style="dim", highlight=False, markup=False)

    def pause(self, message: str) -> None:
        out.print(Panel(Text(message), title="À vous", border_style="yellow", expand=False))
        typer.prompt("Appuyez sur Entrée une fois fait", default="", show_default=False)

    def ask(self, message: str) -> str:
        answer: str = typer.prompt(message, default="", show_default=False)
        return answer


def _browser_profile_dir() -> Path:
    return data_dir() / "browser-profile"


@app.command()
def submit(
    broker: Annotated[
        str | None, typer.Option("--broker", help="Seulement ce broker (slug).")
    ] = None,
    headless: Annotated[
        bool, typer.Option(help="Navigateur invisible (déconseillé : CAPTCHA impossibles).")
    ] = False,
    brokers_dir: BrokersDir = None,
) -> None:
    """Remplir les formulaires web en attente, dans un navigateur visible."""
    config = _config()
    brokers = {b.slug: b for b in _registry(brokers_dir)}

    def open_page() -> AbstractContextManager[Page]:
        return open_browser(_browser_profile_dir(), headless=headless)

    with _tracker() as tracker:
        try:
            report = submit_web_forms(
                tracker,
                brokers,
                config,
                now,
                open_page=open_page,
                prompter=ConsolePrompter(),
                confirm=lambda question: typer.confirm(question, default=False),
                open_url=webbrowser.open,
                proofs_dir=data_dir() / "screenshots",
                only=broker,
            )
        except BrowserUnavailableError as exc:
            raise _fail(str(exc)) from None
    for request_id, proof in report.proofs:
        out.print(f"  n° {request_id} : preuve {proof}", highlight=False, soft_wrap=True)
    for warning in report.warnings:
        out.print(f"[yellow]Attention :[/] {warning}", highlight=False)
    _print_report(report, "formulaire(s) envoyé(s)")
    if report.errors:
        raise typer.Exit(code=1)


@app.command()
def verify(
    broker: Annotated[str, typer.Option("--broker", help="Slug du broker à vérifier.")],
    headed: Annotated[bool, typer.Option(help="Afficher le navigateur.")] = False,
    brokers_dir: BrokersDir = None,
) -> None:
    """Vérifier, sans aucune donnée personnelle, qu'un formulaire automatisé existe encore."""
    found = {b.slug: b for b in _registry(brokers_dir)}.get(broker)
    channel = web_form_channel(found) if found else None
    if channel is None or not channel.steps:
        raise _fail(f"{broker} n'a pas de formulaire automatisé")
    try:
        with open_browser(_browser_profile_dir(), headless=not headed) as page:
            result = verify_form(channel, page)
    except BrowserUnavailableError as exc:
        raise _fail(str(exc)) from None
    for item in result.checked:
        out.print(f"  [green]✓[/] {item}", highlight=False)
    for item in result.missing:
        out.print(f"  [red]✗[/] {item}", highlight=False)
    if result.stopped_at:
        out.print(f"Vérification arrêtée à l'étape {result.stopped_at} (interaction requise).")
    if result.missing:
        raise _fail("le formulaire a changé : mettre à jour la fiche")


@app.command()
def remind(
    request_id: Annotated[int, typer.Argument(help="Numéro de la demande en retard.")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Afficher sans rien écrire.")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Ne pas demander confirmation.")] = False,
    brokers_dir: BrokersDir = None,
) -> None:
    """Relancer un broker qui n'a pas répondu dans le délai légal."""
    config = _config()
    brokers = {b.slug: b for b in _registry(brokers_dir)}
    smtp_mode = config.email.mode is EmailMode.SMTP and not dry_run
    sender: Callable[[], SmtpSender] | None = None
    if smtp_mode:
        smtp = config.email.smtp
        assert smtp is not None  # noqa: S101 - guaranteed by EmailConfig
        try:
            password = smtp_password(smtp)
        except ConfigError as exc:
            raise _fail(str(exc)) from None

        def sender() -> SmtpSender:
            return SmtpSender(smtp, password, delay_seconds=config.email.delay_seconds)

    with _tracker() as tracker:
        if smtp_mode and not yes and not typer.confirm(f"Envoyer la relance n° {request_id} ?"):
            raise typer.Exit(code=1)
        try:
            report = send_reminder(
                tracker,
                brokers,
                config,
                now,
                request_id,
                outbox=data_dir() / "outbox",
                dry_run=dry_run,
                sender=sender,
            )
        except (ReminderError, TrackerError, SendError) as exc:
            raise _fail(str(exc)) from None
    out.print(f"{report.text.subject}", highlight=False, markup=False)
    if dry_run:
        out.print(Text(report.text.body), style="dim")
    elif report.sent:
        out.print("Relance envoyée.")
    elif report.path is not None:
        out.print(f"Relance préparée : {report.path}", highlight=False, soft_wrap=True)


@app.command()
def ui(
    port: Annotated[int, typer.Option(min=1024, max=65535, help="Port local.")] = 8765,
    browser: Annotated[bool, typer.Option(help="Ouvrir le navigateur.")] = True,
    brokers_dir: BrokersDir = None,
) -> None:
    """Interface web locale (127.0.0.1 seulement, protégée par un jeton)."""
    try:
        import uvicorn

        from gomme.ui.app import UiContext, create_app, new_token
    except ImportError:
        raise _fail("l'interface web demande : uv sync --extra ui") from None
    _config()  # fail early, with the usual message, when there is no configuration
    registry = _registry(brokers_dir)
    token = new_token()
    context = UiContext(
        load_config=load_config,
        load_brokers=lambda: registry,
        open_tracker=_tracker,
        clock=now,
        outbox=data_dir() / "outbox",
        token=token,
        port=port,
    )
    url = f"http://127.0.0.1:{port}/login?token={token}"
    out.print(
        "Interface Gomme, sur cette machine uniquement. Adresse (ne la partagez pas) :\n"
        f"  {url}\nCtrl+C pour arrêter.",
        highlight=False,
        soft_wrap=True,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(context),
            host="127.0.0.1",
            port=port,
            # No access log: the login URL carries the token.
            access_log=False,
            log_level="warning",
        )
    )
    launcher = data_dir() / "ui-login.html"
    if browser:
        # The browser gets a private local file that redirects to the login URL: the token
        # never appears on a command line, which other users of the machine could read.
        write_private_file(
            launcher,
            f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" '
            f'content="0; url={url}"><title>Gomme</title>',
        )

        def open_when_ready() -> None:
            while not server.started and not server.should_exit:
                time.sleep(0.1)
            if server.started:  # only once our server holds the port
                webbrowser.open(launcher.as_uri())

        threading.Thread(target=open_when_ready, daemon=True).start()
    try:
        server.run()
    finally:
        launcher.unlink(missing_ok=True)
