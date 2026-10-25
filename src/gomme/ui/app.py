"""Local web interface (`gomme ui`): the same work as the CLI, in a browser.

Only for the person running it, on their machine:
- it listens on 127.0.0.1 only, and checks the Host header (DNS rebinding);
- a random token, printed by `gomme ui`, opens a session (cookie SameSite=Strict); without
  it every page answers 403, even to other users of the same machine. Limit: browsers do
  not separate cookies by port, so a page served by another local user on 127.0.0.1 could
  read the cookie; do not browse such pages while `gomme ui` runs;
- every form carries a CSRF token, and a cross-site Origin is refused;
- no JavaScript and no external resource (strict Content-Security-Policy);
- sending through SMTP stays in the command line, with its confirmation.
"""

import hashlib
import hmac
import secrets
from collections import Counter
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote

import jinja2
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from gomme.brokers.schema import Broker, Region
from gomme.config import Config, ConfigError, EmailMode
from gomme.labels import ACTION_LABELS, MANUAL_ACTIONS, STATUS_LABELS
from gomme.tracker.db import Tracker, TrackerError
from gomme.tracker.state import Action, InvalidTransitionError, allowed_actions
from gomme.workflow import (
    default_selection,
    monitor,
    queue_requests,
    send_emails,
    web_form_channel,
)

SESSION_COOKIE = "gomme_session"
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
}


@dataclass(frozen=True)
class UiContext:
    load_config: Callable[[], Config]
    load_brokers: Callable[[], tuple[Broker, ...]]
    open_tracker: Callable[[], AbstractContextManager[Tracker]]
    clock: Callable[[], datetime]
    outbox: Path
    token: str
    port: int

    @property
    def csrf(self) -> str:
        return hmac.new(self.token.encode(), b"csrf", hashlib.sha256).hexdigest()

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return frozenset({f"127.0.0.1:{self.port}", f"localhost:{self.port}"})


def new_token() -> str:
    return secrets.token_urlsafe(32)


_templates = jinja2.Environment(
    loader=jinja2.PackageLoader("gomme.ui", "templates"),
    autoescape=True,
    undefined=jinja2.StrictUndefined,
)


def create_app(ctx: UiContext) -> Starlette:
    def page(request: Request, name: str, **values: Any) -> HTMLResponse:
        html = _templates.get_template(name).render(
            csrf=ctx.csrf,
            message=request.query_params.get("msg", ""),
            status_labels=STATUS_LABELS,
            action_labels=ACTION_LABELS,
            **values,
        )
        return HTMLResponse(html)

    def back(path: str, message: str) -> RedirectResponse:
        return RedirectResponse(f"{path}?msg={quote(message)}", status_code=303)

    def config_or_none() -> Config | None:
        try:
            return ctx.load_config()
        except ConfigError:
            return None

    no_config = "Configuration absente ou invalide : lancer gomme init dans un terminal."

    def brokers_by_slug() -> dict[str, Broker]:
        return {broker.slug: broker for broker in ctx.load_brokers()}

    async def form(request: Request) -> dict[str, str]:
        # Parsed here rather than with python-multipart: one small dependency less.
        body = (await request.body()).decode("utf-8", errors="replace")
        return {key: values[-1] for key, values in parse_qs(body).items()}

    # --- pages --------------------------------------------------------------------------

    async def dashboard(request: Request) -> Response:
        brokers = brokers_by_slug()
        today = ctx.clock().date()
        with ctx.open_tracker() as tracker:
            requests = tracker.requests()
        rows = []
        for item in requests:
            deadline = item.deadline(brokers.get(item.broker_slug))
            rows.append(
                {
                    "request": item,
                    "due": deadline.due if deadline else None,
                    "days_left": deadline.days_left(today) if deadline else None,
                }
            )
        counts = Counter(item.status for item in requests)
        return page(request, "dashboard.html.j2", rows=rows, counts=counts)

    async def broker_list(request: Request) -> Response:
        region = request.query_params.get("region", "")
        query = request.query_params.get("q", "").strip().lower()
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
            regions=[r.value for r in Region],
        )

    async def broker_detail(request: Request) -> Response:
        broker = brokers_by_slug().get(request.path_params["slug"])
        if broker is None:
            return page(request, "error.html.j2", error="Broker inconnu.")
        with ctx.open_tracker() as tracker:
            latest = tracker.latest_for(broker.slug)
        return page(
            request,
            "broker.html.j2",
            broker=broker,
            latest=latest,
            form=web_form_channel(broker),
        )

    async def request_detail(request: Request) -> Response:
        request_id = int(request.path_params["id"])
        with ctx.open_tracker() as tracker:
            try:
                item = tracker.get(request_id)
            except TrackerError as exc:
                return page(request, "error.html.j2", error=str(exc))
            events = tracker.events(request_id)
        broker = brokers_by_slug().get(item.broker_slug)
        deadline = item.deadline(broker)
        actions = [a for a in allowed_actions(item.status) if a in MANUAL_ACTIONS]
        return page(
            request,
            "request.html.j2",
            item=item,
            broker=broker,
            events=events,
            deadline=deadline,
            actions=actions,
            has_eml=_eml_path(ctx.outbox, item.id, item.broker_slug).is_file(),
        )

    async def profile(request: Request) -> Response:
        try:
            config = ctx.load_config()
        except ConfigError as exc:
            return page(request, "error.html.j2", error=str(exc))
        return page(
            request, "profile.html.j2", summary=config.profile.masked(), mode=config.email.mode
        )

    async def send_preview(request: Request) -> Response:
        try:
            config = ctx.load_config()
        except ConfigError as exc:
            return page(request, "error.html.j2", error=str(exc))
        with ctx.open_tracker() as tracker:
            report = send_emails(
                tracker, brokers_by_slug(), config, ctx.clock, outbox=ctx.outbox, dry_run=True
            )
        return page(
            request,
            "send.html.j2",
            previews=report.previews,
            skipped=report.skipped,
            manual=config.email.mode is EmailMode.MANUAL,
            limit_reached=report.limit_reached,
        )

    async def eml_download(request: Request) -> Response:
        request_id = int(request.path_params["id"])
        with ctx.open_tracker() as tracker:
            try:
                item = tracker.get(request_id)
            except TrackerError:
                return Response(status_code=404)
        path = _eml_path(ctx.outbox, item.id, item.broker_slug)
        if not path.is_file():
            return Response(status_code=404)
        return FileResponse(path, media_type="message/rfc822", filename=path.name)

    # --- actions (POST) -----------------------------------------------------------------

    async def queue_default(request: Request) -> Response:
        config = config_or_none()
        if config is None:
            return back("/", no_config)
        _, selected = default_selection(ctx.load_brokers(), config.profile)
        with ctx.open_tracker() as tracker:
            report = queue_requests(tracker, selected, config, ctx.clock())
        return back(
            "/", f"{len(report.done)} demande(s) mise(s) en file, {len(report.skipped)} écartée(s)."
        )

    async def queue_one(request: Request) -> Response:
        slug = request.path_params["slug"]
        broker = brokers_by_slug().get(slug)
        if broker is None:
            return back("/brokers", "Broker inconnu.")
        config = config_or_none()
        if config is None:
            return back(f"/brokers/{slug}", no_config)
        with ctx.open_tracker() as tracker:
            report = queue_requests(tracker, [broker], config, ctx.clock())
        message = "Demande mise en file." if report.done else f"Écartée : {report.skipped[0][1]}"
        return back(f"/brokers/{slug}", message)

    async def mark(request: Request) -> Response:
        request_id = int(request.path_params["id"])
        values = await form(request)
        try:
            action = Action(values.get("action", ""))
        except ValueError:
            return back(f"/requests/{request_id}", "Action inconnue.")
        if action not in MANUAL_ACTIONS:
            return back(f"/requests/{request_id}", "Action réservée à gomme monitor ou escalate.")
        with ctx.open_tracker() as tracker:
            try:
                tracker.apply(request_id, action, ctx.clock(), note=values.get("note", "")[:500])
            except (InvalidTransitionError, TrackerError) as exc:
                return back(f"/requests/{request_id}", str(exc))
        return back(f"/requests/{request_id}", f"Noté : {ACTION_LABELS[action]}.")

    async def receipt(request: Request) -> Response:
        request_id = int(request.path_params["id"])
        values = await form(request)
        try:
            day = date.fromisoformat(values.get("received_on", ""))
        except ValueError:
            return back(f"/requests/{request_id}", "Date invalide (AAAA-MM-JJ).")
        with ctx.open_tracker() as tracker:
            try:
                current = tracker.get(request_id)
                tracker.record_receipt(
                    request_id, day, ctx.clock(), brokers_by_slug().get(current.broker_slug)
                )
            except TrackerError as exc:
                return back(f"/requests/{request_id}", str(exc))
        return back(f"/requests/{request_id}", f"Réception notée : {day.isoformat()}.")

    async def write_emails(request: Request) -> Response:
        config = config_or_none()
        if config is None:
            return back("/send", no_config)
        if config.email.mode is not EmailMode.MANUAL:
            return back("/send", "En mode smtp, l'envoi se fait en ligne de commande : gomme send.")
        with ctx.open_tracker() as tracker:
            report = send_emails(
                tracker, brokers_by_slug(), config, ctx.clock, outbox=ctx.outbox, dry_run=False
            )
        return back(
            "/",
            f"{len(report.files)} fichier(s) .eml préparé(s) : téléchargez-les depuis chaque "
            "demande, envoyez-les, puis notez l'envoi.",
        )

    async def run_monitor(request: Request) -> Response:
        config = config_or_none()
        if config is None:
            return back("/", no_config)
        with ctx.open_tracker() as tracker:
            report = monitor(tracker, brokers_by_slug(), config, ctx.clock())
        return back("/", f"Suivi : {len(report.done)} changement(s).")

    routes = [
        Route("/", dashboard),
        Route("/brokers", broker_list),
        Route("/brokers/{slug}", broker_detail),
        Route("/brokers/{slug}/queue", queue_one, methods=["POST"]),
        Route("/requests/{id:int}", request_detail),
        Route("/requests/{id:int}/eml", eml_download),
        Route("/requests/{id:int}/mark", mark, methods=["POST"]),
        Route("/requests/{id:int}/received", receipt, methods=["POST"]),
        Route("/profile", profile),
        Route("/send", send_preview),
        Route("/send", write_emails, methods=["POST"]),
        Route("/queue", queue_default, methods=["POST"]),
        Route("/monitor", run_monitor, methods=["POST"]),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(_Guard, ctx=ctx)
    return app


def _same(given: str, expected: str) -> bool:
    """Constant-time comparison; bytes, so any non-ASCII input is simply unequal."""
    return hmac.compare_digest(given.encode(), expected.encode())


def _eml_path(outbox: Path, request_id: int, slug: str) -> Path:
    return outbox / f"{request_id}-{slug}.eml"


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
            return Response("Hôte refusé.", status_code=400)
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
                return Response("Origine refusée.", status_code=403)
            values = parse_qs(body.decode("utf-8", errors="replace"))
            if not _same(values.get("csrf", [""])[-1], ctx.csrf):
                return Response("Jeton de formulaire invalide.", status_code=403)
        return None


_DENIED = (
    "<!doctype html><meta charset=utf-8><title>Gomme</title>"
    "<p>Accès refusé. Ouvrez l'adresse affichée par <code>gomme ui</code> dans le terminal.</p>"
)
