"""Local web interface (`gomme ui`): everything a non-developer needs, in a browser.

Only for the person running it, on their machine:
- it listens on 127.0.0.1 only, and checks the Host header (DNS rebinding);
- a random token, printed by `gomme ui`, opens a session (cookie SameSite=Strict); without
  it every page answers 403, even to other users of the same machine. Limit: browsers do
  not separate cookies by port, so a page served by another local user on 127.0.0.1 could
  read the cookie; do not browse such pages while `gomme ui` runs;
- every form carries a CSRF token, and a cross-site Origin is refused;
- no JavaScript and no external resource (strict Content-Security-Policy);
- emails are sent by the user from their own mailbox (Gmail compose window or mail
  application); sending through SMTP stays in the command line, with its confirmation.
"""

import hashlib
import hmac
import os
import re
import secrets
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote

import jinja2
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from gomme.brokers.fields import ProfileField
from gomme.brokers.schema import Broker, Region
from gomme.channels.email import (
    SendError,
    SmtpFactory,
    SmtpSender,
    build_message,
    connect,
    gmail_compose_link,
    mailto_link,
)
from gomme.config import (
    Composer,
    Config,
    ConfigError,
    EmailMode,
    PasswordStoreError,
    SmtpConfig,
    SystemKeyring,
)
from gomme.labels import (
    ACTION_BUTTONS,
    ACTION_LABELS,
    CATEGORY_LABELS,
    CHANNEL_LABELS,
    FIELD_LABELS,
    JURISDICTION_LABELS,
    MANUAL_ACTIONS,
    REGION_LABELS,
    STATUS_LABELS,
)
from gomme.privacy import mask_email
from gomme.profile import US_STATES
from gomme.tracker.db import Tracker, TrackerError
from gomme.tracker.state import Action, InvalidTransitionError, Status, allowed_actions
from gomme.ui import forms
from gomme.ui.sending import Jobs, PasswordStore, SendJob, explain_failure, in_background
from gomme.ui.views import STATUS_TONES, board, countdown, french_date, plural, scope, selection
from gomme.workflow import (
    Prepared,
    Report,
    default_selection,
    monitor,
    prepare_emails,
    queue_requests,
    send_emails,
    smtp_quota,
    web_form_channel,
    write_letters,
)

SESSION_COOKIE = "gomme_session"
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    # Not "no-referrer": browsers then send "Origin: null" with every form, which the
    # Origin check below must refuse. "same-origin" keeps the Origin for our own forms
    # and still sends nothing to other sites (Gmail, brokers).
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
}

# Where a form may send the user back to, after an action.
_SAFE_NEXT = re.compile(r"^/(send|profile|requests/\d{1,18})?$")

# Button style of each action on a request page.
_ACTION_STYLES = {
    Action.SEND: "primary",
    Action.COMPLETE: "primary",
    Action.ACTION_DONE: "primary",
    Action.CONFIRM_REMOVED: "primary",
    Action.REFUSE: "danger",
    Action.UNQUEUE: "ghost",
}
# History labels of the web interface, where they differ from the command line's.
_EVENT_LABELS = {
    "create": "demande créée",
    "note": "note",
    Action.QUEUE.value: "prête à envoyer",
    Action.UNQUEUE.value: "retirée de la file d'envoi",
    Action.SEND.value: "envoyée",
}


@dataclass(frozen=True)
class UiContext:
    load_config: Callable[[], Config]
    save_config: Callable[[Config], object]
    config_exists: Callable[[], bool]
    load_brokers: Callable[[], tuple[Broker, ...]]
    open_tracker: Callable[[], AbstractContextManager[Tracker]]
    clock: Callable[[], datetime]
    outbox: Path
    letters_dir: Path
    token: str
    port: int
    passwords: PasswordStore = field(default_factory=SystemKeyring)
    smtp_factory: SmtpFactory = connect
    run_background: Callable[[Callable[[], None]], None] = in_background

    @property
    def csrf(self) -> str:
        return hmac.new(self.token.encode(), b"csrf", hashlib.sha256).hexdigest()

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return frozenset({f"127.0.0.1:{self.port}", f"localhost:{self.port}"})


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _event_label(action: str) -> str:
    if action in _EVENT_LABELS:
        return _EVENT_LABELS[action]
    try:
        return ACTION_LABELS[Action(action)]
    except ValueError:
        return action


_templates = jinja2.Environment(
    loader=jinja2.PackageLoader("gomme.ui", "templates"),
    autoescape=True,
    undefined=jinja2.StrictUndefined,
)
_templates.filters |= {
    "fr_date": french_date,
    "countdown": countdown,
    "event_label": _event_label,
    "mask_email": lambda value: mask_email(value) if "@" in value else "***",
}
_templates.globals |= {
    "status_labels": STATUS_LABELS,
    "status_tones": STATUS_TONES,
    "action_buttons": ACTION_BUTTONS,
    "action_styles": _ACTION_STYLES,
    "channel_labels": CHANNEL_LABELS,
    "jurisdiction_labels": JURISDICTION_LABELS,
    "field_labels": FIELD_LABELS,
    "category_labels": CATEGORY_LABELS,
    "region_labels": REGION_LABELS,
    "countries": forms.COUNTRIES,
    "field_keys": ProfileField,
    "action_send": Action.SEND,
    "is_gmail": forms.is_gmail,
    "sending_labels": {
        "smtp": "Envoi automatique",
        "gmail": "Une par une, dans Gmail",
        "mail_app": "Une par une, dans le logiciel de messagerie",
    },
}


def compose_link(item: Prepared, config: Config) -> str:
    if config.email.composer is Composer.GMAIL:
        return gmail_compose_link(item.text, item.to, account=config.profile.email)
    return mailto_link(item.text, item.to)


def create_app(ctx: UiContext) -> Starlette:
    def page(request: Request, template: str, *, status_code: int = 200, **values: Any) -> Response:
        to_send = 0
        if ctx.config_exists():
            try:
                with ctx.open_tracker() as tracker:
                    to_send = len(tracker.requests(status=Status.QUEUED))
            except (TrackerError, OSError):
                to_send = 0
        html = _templates.get_template(template).render(
            csrf=ctx.csrf,
            message=request.query_params.get("msg", "")[:500],
            path=request.url.path,
            to_send_count=to_send,
            **values,
        )
        return HTMLResponse(html, status_code=status_code)

    def error(request: Request, text: str, status_code: int = 200) -> Response:
        return page(request, "error.html.j2", error=text, status_code=status_code)

    def back(path: str, message: str) -> RedirectResponse:
        return RedirectResponse(f"{path}?msg={quote(message)}", status_code=303)

    def current_config(request: Request) -> Config | Response:
        """The configuration, or the page to show instead (first-run wizard, or error)."""
        if not ctx.config_exists():
            return RedirectResponse("/setup", status_code=303)
        try:
            return ctx.load_config()
        except ConfigError as exc:
            return error(request, f"Configuration illisible : {exc}")

    def brokers_by_slug() -> dict[str, Broker]:
        return {broker.slug: broker for broker in ctx.load_brokers()}

    jobs = Jobs()

    def check_and_keep(smtp: SmtpConfig, typed: str) -> tuple[str | None, str]:
        """Log in once with the typed password (nothing is sent), then keep it in the
        keyring. Returns (error for the field, note for the user)."""
        password = typed.replace(" ", "")  # Gmail shows app passwords in groups of four
        note = "Mot de passe vérifié et gardé dans le trousseau sécurisé de cet ordinateur."
        try:
            with SmtpSender(smtp, password, factory=ctx.smtp_factory):
                pass
        except SendError as exc:
            if "Authentication" in str(exc):
                return explain_failure(str(exc)), ""
            note = (
                "Connexion au serveur impossible pour le vérifier maintenant ; "
                "il est gardé dans le trousseau quand même."
            )
        try:
            ctx.passwords.set(smtp.username, password)
        except PasswordStoreError as exc:
            note = f"Trousseau indisponible ({exc}) : le mot de passe sera demandé à chaque envoi."
        return None, note

    def password_saved_for(values: dict[str, str]) -> bool:
        username = values.get("smtp_username", "")
        return bool(username and ctx.passwords.get(username))

    def known_password(smtp: SmtpConfig) -> str | None:
        """The SMTP password from the environment or the keyring, if any."""
        return os.environ.get(smtp.password_env) or ctx.passwords.get(smtp.username)

    async def form(request: Request) -> dict[str, str]:
        # Parsed here rather than with python-multipart: one small dependency less.
        body = (await request.body()).decode("utf-8", errors="replace")
        return {key: values[-1] for key, values in parse_qs(body).items()}

    def today() -> date:
        return ctx.clock().date()

    # --- first run and profile ----------------------------------------------------------

    def setup_page(
        request: Request,
        step: forms.Step,
        values: dict[str, str],
        errors: dict[str, str] | None = None,
    ) -> Response:
        if step.name == "sending":
            values = forms.with_sending_defaults(values)
        return page(
            request,
            "setup.html.j2",
            step=step,
            steps=forms.STEPS,
            step_index=forms.STEPS.index(step),
            values=values,
            errors=errors or {},
            hidden={k: v for k, v in values.items() if k not in step.fields and v},
            needs=forms.fields_needed(ctx.load_brokers(), values.get("country") or "FR"),
            us_states=US_STATES,
            password_saved=password_saved_for(values),
        )

    async def setup(request: Request) -> Response:
        if ctx.config_exists():
            return RedirectResponse("/", status_code=303)
        values = forms.clean({"country": "FR"})
        return setup_page(request, forms.STEPS[0], values)

    async def setup_post(request: Request) -> Response:
        if ctx.config_exists():
            return back("/profile", "Votre profil existe déjà : modifiez-le ici.")
        submitted = await form(request)
        values = forms.clean(submitted)
        step = forms.step_named(submitted.get("step", ""))
        go = submitted.get("go", "next")
        if go == "back":
            return setup_page(request, forms.next_step(step, -1), values)
        if go == "save":
            errors = forms.errors_for(values, forms.FIELDS, today())
            if errors:
                first = next(s for s in forms.STEPS if set(s.fields) & errors.keys())
                return setup_page(request, first, values, errors)
            ctx.save_config(forms.build_config(values))
            return back("/", "Profil enregistré. Vous pouvez préparer vos demandes.")
        errors = forms.errors_for(values, step.fields, today())
        if errors:
            return setup_page(request, step, values, errors)
        typed = submitted.get("smtp_password", "")
        if step.name == "sending" and values["sending"] == "smtp" and typed:
            problem, _ = await run_in_threadpool(check_and_keep, forms.smtp_settings(values), typed)
            if problem:
                return setup_page(request, step, values, {"smtp_password": problem})
        return setup_page(request, forms.next_step(step, 1), values)

    async def profile(request: Request) -> Response:
        config = current_config(request)
        if isinstance(config, Response):
            return config
        smtp = config.email.smtp
        return page(
            request,
            "profile.html.j2",
            summary=config.profile.masked(),
            email=config.email,
            password_saved=bool(smtp and ctx.passwords.get(smtp.username)),
        )

    def profile_form(
        request: Request, values: dict[str, str], errors: dict[str, str] | None = None
    ) -> Response:
        return page(
            request,
            "profile_edit.html.j2",
            values=forms.with_sending_defaults(values),
            errors=errors or {},
            needs=forms.fields_needed(ctx.load_brokers(), values.get("country") or "FR"),
            us_states=US_STATES,
            password_saved=password_saved_for(forms.with_sending_defaults(values)),
        )

    async def profile_edit(request: Request) -> Response:
        config = current_config(request)
        if isinstance(config, Response):
            return config
        return profile_form(request, forms.values_of(config))

    async def profile_post(request: Request) -> Response:
        config = current_config(request)
        if isinstance(config, Response):
            return config
        submitted = await form(request)
        values = forms.clean(submitted)
        errors = forms.errors_for(values, forms.FIELDS, today())
        if errors:
            return profile_form(request, values, errors)
        updated = forms.build_config(values, base=config)
        note = ""
        typed = submitted.get("smtp_password", "")
        if updated.email.mode is EmailMode.SMTP and updated.email.smtp and typed:
            problem, note = await run_in_threadpool(check_and_keep, updated.email.smtp, typed)
            if problem:
                return profile_form(request, values, {"smtp_password": problem})
        ctx.save_config(updated)
        return back("/profile", f"Profil mis à jour. {note}".strip())

    # --- pages --------------------------------------------------------------------------

    async def dashboard(request: Request) -> Response:
        config = current_config(request)
        if isinstance(config, Response):
            return config
        with ctx.open_tracker() as tracker:
            requests = tracker.requests()
        brokers = brokers_by_slug()
        return page(
            request,
            "dashboard.html.j2",
            board=board(requests, brokers, today()),
            selection=selection(
                brokers.values(), config.profile, already=[r.broker_slug for r in requests]
            ),
            first_name=config.profile.first_name,
            scope=scope(config.profile.country),
        )

    async def broker_list(request: Request) -> Response:
        region = request.query_params.get("region", "")
        query = request.query_params.get("q", "").strip().lower()[:100]
        brokers = [
            broker
            for broker in ctx.load_brokers()
            if (not region or broker.region == region)
            and (not query or query in broker.name.lower() or query in broker.slug)
        ]
        return page(
            request,
            "brokers.html.j2",
            brokers=brokers[:300],
            total=len(brokers),
            region=region,
            query=query,
            regions=list(Region),
        )

    async def broker_detail(request: Request) -> Response:
        broker = brokers_by_slug().get(request.path_params["slug"])
        if broker is None:
            return error(request, "Broker inconnu.", 404)
        with ctx.open_tracker() as tracker:
            latest = tracker.latest_for(broker.slug)
        return page(
            request,
            "broker.html.j2",
            broker=broker,
            latest=latest,
            form=web_form_channel(broker),
        )

    def queued_email(tracker: Tracker, config: Config, request_id: int) -> Prepared | None:
        prepared = prepare_emails(tracker, brokers_by_slug(), config, Report())
        return next((item for item in prepared if item.request.id == request_id), None)

    async def request_detail(request: Request) -> Response:
        config = current_config(request)
        if isinstance(config, Response):
            return config
        request_id = int(request.path_params["id"])
        with ctx.open_tracker() as tracker:
            try:
                item = tracker.get(request_id)
            except TrackerError as exc:
                return error(request, str(exc), 404)
            events = tracker.events(request_id)
            email = queued_email(tracker, config, request_id)
        broker = brokers_by_slug().get(item.broker_slug)
        deadline = item.deadline(broker)
        web_form = web_form_channel(broker) if broker else None
        return page(
            request,
            "request.html.j2",
            item=item,
            broker=broker,
            name=broker.name if broker else item.broker_slug,
            events=events,
            deadline=deadline,
            days_left=deadline.days_left(today()) if deadline else None,
            actions=[a for a in allowed_actions(item.status) if a in MANUAL_ACTIONS],
            email=email,
            link=compose_link(email, config) if email else None,
            composer=config.email.composer,
            has_letter=_letter_path(ctx.letters_dir, item.id, item.broker_slug).is_file(),
            form_url=web_form.url if web_form else None,
        )

    async def send_page(request: Request) -> Response:
        config = current_config(request)
        if isinstance(config, Response):
            return config
        brokers = brokers_by_slug()
        skipped = Report()
        smtp = config.email.smtp
        with ctx.open_tracker() as tracker:
            emails = prepare_emails(tracker, brokers, config, skipped)
            queued = tracker.requests(status=Status.QUEUED)
            quota = smtp_quota(tracker, config, today())
        letters = [
            {
                "request": r,
                "broker": brokers.get(r.broker_slug),
                "ready": _letter_path(ctx.letters_dir, r.id, r.broker_slug).is_file(),
            }
            for r in queued
            if r.channel == "postal"
        ]
        web_forms = []
        for r in queued:
            if r.channel != "web_form":
                continue
            broker = brokers.get(r.broker_slug)
            channel = web_form_channel(broker) if broker else None
            web_forms.append({"request": r, "broker": broker, "channel": channel})
        return page(
            request,
            "send.html.j2",
            emails=[(item, compose_link(item, config)) for item in emails],
            letters=letters,
            web_forms=web_forms,
            skipped=skipped.skipped,
            composer=config.email.composer,
            smtp=config.email.smtp if config.email.mode is EmailMode.SMTP else None,
            password_known=bool(smtp and known_password(smtp)),
            keyring_available=ctx.passwords.available(),
            quota=quota,
            daily_limit=config.email.daily_limit,
            delay=round(config.email.delay_seconds),
            sending_now=jobs.running(),
        )

    async def eml_download(request: Request) -> Response:
        request_id = int(request.path_params["id"])
        config = current_config(request)
        if isinstance(config, Response):
            return Response(status_code=404)
        with ctx.open_tracker() as tracker:
            try:
                item = tracker.get(request_id)
            except TrackerError:
                return Response(status_code=404)
            email = queued_email(tracker, config, request_id)
        filename = f"{item.id}-{item.broker_slug}.eml"
        path = ctx.outbox / filename
        if path.is_file():
            return FileResponse(path, media_type="message/rfc822", filename=filename)
        if email is None:
            return Response(status_code=404)
        message = build_message(
            email.text,
            to=email.to,
            sender_name=config.profile.full_name,
            sender_address=config.profile.email,
            now=ctx.clock(),
        )
        return Response(
            message.as_bytes(),
            media_type="message/rfc822",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    async def letter_download(request: Request) -> Response:
        request_id = int(request.path_params["id"])
        with ctx.open_tracker() as tracker:
            try:
                item = tracker.get(request_id)
            except TrackerError:
                return Response(status_code=404)
        path = _letter_path(ctx.letters_dir, item.id, item.broker_slug)
        if not path.is_file():
            return Response(status_code=404)
        return FileResponse(path, media_type="application/pdf", filename=path.name)

    # --- actions (POST) -----------------------------------------------------------------

    async def queue_default(request: Request) -> Response:
        config = current_config(request)
        if isinstance(config, Response):
            return config
        _, selected = default_selection(ctx.load_brokers(), config.profile)
        with ctx.open_tracker() as tracker:
            report = queue_requests(tracker, selected, config, ctx.clock())
        incomplete = sum(1 for _, reason in report.skipped if reason.startswith("profil"))
        message = plural(len(report.done), "demande préparée", "demandes préparées") + "."
        if incomplete:
            waiting = plural(incomplete, "broker attend", "brokers attendent")
            message += f" {waiting} un profil plus complet."
        return back("/", message)

    async def queue_one(request: Request) -> Response:
        slug = request.path_params["slug"]
        broker = brokers_by_slug().get(slug)
        if broker is None:
            return back("/brokers", "Broker inconnu.")
        config = current_config(request)
        if isinstance(config, Response):
            return config
        with ctx.open_tracker() as tracker:
            report = queue_requests(tracker, [broker], config, ctx.clock())
        message = "Demande préparée." if report.done else f"Pas de demande : {report.skipped[0][1]}"
        return back(f"/brokers/{slug}", message)

    async def mark(request: Request) -> Response:
        request_id = int(request.path_params["id"])
        values = await form(request)
        target = values.get("next", "")
        target = target if _SAFE_NEXT.match(target) else f"/requests/{request_id}"
        try:
            action = Action(values.get("action", ""))
        except ValueError:
            return back(target, "Action inconnue.")
        if action not in MANUAL_ACTIONS:
            return back(target, "Action réservée au suivi automatique.")
        with ctx.open_tracker() as tracker:
            try:
                tracker.apply(request_id, action, ctx.clock(), note=values.get("note", "")[:500])
                name = tracker.get(request_id).broker_slug
            except (InvalidTransitionError, TrackerError) as exc:
                return back(target, str(exc))
        broker = brokers_by_slug().get(name)
        return back(
            target, f"Noté pour {broker.name if broker else name} : {ACTION_LABELS[action]}."
        )

    async def receipt(request: Request) -> Response:
        request_id = int(request.path_params["id"])
        values = await form(request)
        try:
            day = date.fromisoformat(values.get("received_on", ""))
        except ValueError:
            return back(f"/requests/{request_id}", "Date invalide.")
        with ctx.open_tracker() as tracker:
            try:
                current = tracker.get(request_id)
                tracker.record_receipt(
                    request_id, day, ctx.clock(), brokers_by_slug().get(current.broker_slug)
                )
            except TrackerError as exc:
                return back(f"/requests/{request_id}", str(exc))
        return back(f"/requests/{request_id}", f"Réception notée : {french_date(day)}.")

    async def make_letters(request: Request) -> Response:
        config = current_config(request)
        if isinstance(config, Response):
            return config
        with ctx.open_tracker() as tracker:
            report = write_letters(
                tracker, brokers_by_slug(), config, today(), letters_dir=ctx.letters_dir
            )
        message = plural(len(report.files), "courrier prêt", "courriers prêts") + " à imprimer."
        if report.skipped:
            message += " Non préparés : " + " ; ".join(f"{s} ({r})" for s, r in report.skipped)
        return back("/send", message)

    async def send_auto(request: Request) -> Response:
        config = current_config(request)
        if isinstance(config, Response):
            return config
        smtp = config.email.smtp
        if config.email.mode is not EmailMode.SMTP or smtp is None:
            return back("/send", "L'envoi automatique n'est pas activé : voir « Mon profil ».")
        if jobs.running():
            return RedirectResponse("/send/progress", status_code=303)
        values = await form(request)
        if values.get("confirm") != "yes":
            return back("/send", "Cochez la case de confirmation pour lancer l'envoi.")
        password = known_password(smtp)
        from_form = password is None
        if from_form:
            password = values.get("password", "").replace(" ", "")  # Gmail shows it in groups
        if not password:
            return back("/send", "Indiquez le mot de passe d'application.")
        remember = from_form and values.get("remember") == "yes"
        brokers = brokers_by_slug()
        with ctx.open_tracker() as tracker:
            prepared = prepare_emails(tracker, brokers, config, Report())
            quota = smtp_quota(tracker, config, today())
        if not prepared:
            return back("/send", "Aucun email à envoyer.")
        if quota == 0:
            return back("/send", "Limite du jour atteinte : la suite partira demain.")
        job = SendJob(
            request_ids=tuple(item.request.id for item in prepared),
            planned=min(len(prepared), quota),
            delay_seconds=config.email.delay_seconds,
            started_at=ctx.clock(),
        )

        def work(job: SendJob) -> None:
            def session() -> SmtpSender:
                return SmtpSender(
                    smtp,
                    password,
                    factory=ctx.smtp_factory,
                    delay_seconds=config.email.delay_seconds,
                )

            with ctx.open_tracker() as tracker:
                report = send_emails(
                    tracker,
                    brokers,
                    config,
                    ctx.clock,
                    outbox=ctx.outbox,
                    dry_run=False,
                    sender=session,
                )
            job.sent = len(report.done)
            job.limit_reached = report.limit_reached
            session_errors = [e for e in report.errors if not e.startswith("n° ")]
            job.errors = [e for e in report.errors if e.startswith("n° ")]
            if session_errors:
                job.failure = explain_failure(session_errors[0])
            if remember and report.done:
                try:
                    ctx.passwords.set(smtp.username, password)
                    job.keyring_note = "Mot de passe gardé dans le trousseau du système."
                except PasswordStoreError as exc:
                    job.keyring_note = f"Mot de passe non gardé : {exc}."

        jobs.start(job, work, ctx.run_background)
        return RedirectResponse("/send/progress", status_code=303)

    async def send_progress(request: Request) -> Response:
        job = jobs.current
        if job is None:
            return RedirectResponse("/send", status_code=303)
        with ctx.open_tracker() as tracker:
            done = sum(
                1
                for request_id in job.request_ids
                if tracker.get(request_id).status is not Status.QUEUED
            )
        remaining = max(job.planned - done, 0)
        return page(
            request,
            "progress.html.j2",
            job=job,
            done=min(done, job.planned),
            percent=round(100 * min(done, job.planned) / job.planned) if job.planned else 100,
            minutes_left=max(1, round(remaining * job.delay_seconds / 60)),
        )

    async def forget_password(request: Request) -> Response:
        config = current_config(request)
        if isinstance(config, Response):
            return config
        if config.email.smtp is not None:
            ctx.passwords.delete(config.email.smtp.username)
        return back("/profile", "Mot de passe retiré du trousseau.")

    async def run_monitor(request: Request) -> Response:
        config = current_config(request)
        if isinstance(config, Response):
            return config
        with ctx.open_tracker() as tracker:
            report = monitor(tracker, brokers_by_slug(), config, ctx.clock())
        if not report.done:
            return back("/", "Délais vérifiés : rien de nouveau.")
        updated = plural(len(report.done), "demande mise", "demandes mises")
        return back("/", f"Délais vérifiés : {updated} à jour.")

    routes = [
        Route("/", dashboard),
        Route("/setup", setup),
        Route("/setup", setup_post, methods=["POST"]),
        Route("/profile", profile),
        Route("/profile", profile_post, methods=["POST"]),
        Route("/profile/edit", profile_edit),
        Route("/brokers", broker_list),
        Route("/brokers/{slug}", broker_detail),
        Route("/brokers/{slug}/queue", queue_one, methods=["POST"]),
        Route("/requests/{id:int}", request_detail),
        Route("/requests/{id:int}/eml", eml_download),
        Route("/requests/{id:int}/letter", letter_download),
        Route("/requests/{id:int}/mark", mark, methods=["POST"]),
        Route("/requests/{id:int}/received", receipt, methods=["POST"]),
        Route("/send", send_page),
        Route("/letters", make_letters, methods=["POST"]),
        Route("/send/auto", send_auto, methods=["POST"]),
        Route("/send/progress", send_progress),
        Route("/smtp/forget", forget_password, methods=["POST"]),
        Route("/queue", queue_default, methods=["POST"]),
        Route("/monitor", run_monitor, methods=["POST"]),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(_Guard, ctx=ctx)
    return app


def _same(given: str, expected: str) -> bool:
    """Constant-time comparison; bytes, so any non-ASCII input is simply unequal."""
    return hmac.compare_digest(given.encode(), expected.encode())


def _letter_path(letters_dir: Path, request_id: int, slug: str) -> Path:
    return letters_dir / f"{request_id}-{slug}.pdf"


class _Guard:
    """Host check, token session, CSRF and security headers, before any route."""

    def __init__(self, app: Callable[..., Awaitable[None]], ctx: UiContext) -> None:
        self.app = app
        self.ctx = ctx

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        body = b""
        if scope.get("method") == "POST":
            # Read the body once, for the CSRF check, then replay it for the route.
            while True:
                message = await receive()
                body += message.get("body", b"")
                if not message.get("more_body", False):
                    break
            replayed = False

            async def replay() -> dict[str, Any]:
                nonlocal replayed
                if replayed:
                    return {"type": "http.disconnect"}
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}

            receive = replay
        request = Request(scope, receive)
        response = await self._check(request, body)
        if response is not None:
            await self._with_headers(response)(scope, receive, send)
            return

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers += [(k.lower().encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    def _with_headers(response: Response) -> Response:
        response.headers.update(SECURITY_HEADERS)
        return response

    async def _check(self, request: Request, body: bytes) -> Response | None:
        ctx = self.ctx
        if request.headers.get("host", "") not in ctx.allowed_hosts:
            return Response("Hôte refusé.", status_code=400, media_type="text/plain")
        if request.url.path == "/login":
            token = request.query_params.get("token", "")
            if not _same(token, ctx.token):
                return Response(_DENIED, status_code=403, media_type="text/html")
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(
                SESSION_COOKIE, ctx.token, httponly=True, samesite="strict", path="/"
            )
            return response
        session = request.cookies.get(SESSION_COOKIE, "")
        if not _same(session, ctx.token):
            return Response(_DENIED, status_code=403, media_type="text/html")
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin is not None and origin.removeprefix("http://") not in ctx.allowed_hosts:
                return Response("Origine refusée.", status_code=403, media_type="text/plain")
            values = parse_qs(body.decode("utf-8", errors="replace"))
            if not _same(values.get("csrf", [""])[-1], ctx.csrf):
                return Response(
                    "Jeton de formulaire invalide.", status_code=403, media_type="text/plain"
                )
        return None


_DENIED = (
    "<!doctype html><meta charset=utf-8><title>Gomme</title>"
    "<body style='font:16px/1.5 system-ui,sans-serif;max-width:36em;"
    "margin:15vh auto;padding:0 16px'>"
    "<h1>Accès réservé</h1><p>Pour ouvrir Gomme, lancez <code>gomme ui</code> : la bonne "
    "adresse s'ouvre toute seule dans votre navigateur.</p>"
)
