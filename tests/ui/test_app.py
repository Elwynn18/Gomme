"""Tests for the local web interface: access control first, then the pages."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeSmtp
from starlette.testclient import TestClient

from gomme.brokers.registry import load_registry
from gomme.config import (
    Composer,
    Config,
    ConfigError,
    EmailConfig,
    EmailMode,
    RequestOptions,
    SmtpConfig,
)
from gomme.legal.statutes import Jurisdiction
from gomme.profile import Profile
from gomme.tracker.db import Tracker
from gomme.tracker.state import Action, Status
from gomme.ui.app import SESSION_COOKIE, UiContext, create_app

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)

IDENTITY = {
    "first_name": "Jeanne",
    "last_name": "Exemple",
    "email": "jeanne.exemple@example.org",
    "country": "FR",
}
ADDRESS = {"street": "1 rue de l'Exemple", "postal_code": "00000", "city": "Nulle-Part"}


class Passwords:
    """The system keyring, in memory."""

    def __init__(self) -> None:
        self.saved: dict[str, str] = {}

    def available(self) -> bool:
        return True

    def get(self, username: str) -> str | None:
        return self.saved.get(username)

    def set(self, username: str, password: str) -> None:
        self.saved[username] = password

    def delete(self, username: str) -> None:
        self.saved.pop(username, None)


class Store:
    """The configuration file, in memory."""

    def __init__(self, config: Config | None) -> None:
        self.config = config
        self.saved = 0

    def load(self) -> Config:
        if self.config is None:
            raise ConfigError("aucune configuration")
        return self.config

    def save(self, config: Config) -> None:
        self.config = config
        self.saved += 1

    def exists(self) -> bool:
        return self.config is not None


@pytest.fixture
def store(profile: Profile) -> Store:
    return Store(Config(profile=profile))


@pytest.fixture
def passwords() -> Passwords:
    return Passwords()


@pytest.fixture
def ctx(
    tmp_path: Path,
    store: Store,
    passwords: Passwords,
    fake_smtp: FakeSmtp,
    fixtures_brokers_dir: Path,
) -> UiContext:
    brokers = load_registry(fixtures_brokers_dir)
    db_path = tmp_path / "gomme.db"

    @contextmanager
    def open_tracker() -> Iterator[Tracker]:
        with Tracker(db_path) as tracker:
            yield tracker

    return UiContext(
        load_config=store.load,
        save_config=store.save,
        config_exists=store.exists,
        load_brokers=lambda: brokers,
        open_tracker=open_tracker,
        clock=lambda: NOW,
        outbox=tmp_path / "outbox",
        letters_dir=tmp_path / "letters",
        token="test-token-not-secret",
        port=PORT,
        passwords=passwords,
        smtp_factory=lambda _config, _context: fake_smtp,
        run_background=lambda work: work(),  # synchronous in tests
    )


@pytest.fixture
def client(ctx: UiContext) -> TestClient:
    """A logged-in client."""
    client = TestClient(create_app(ctx), base_url=BASE)
    response = client.get("/login", params={"token": ctx.token}, follow_redirects=False)
    assert response.status_code == 303
    return client


def post(client: TestClient, ctx: UiContext, path: str, **data: str) -> Any:
    return client.post(path, data={"csrf": ctx.csrf, **data}, follow_redirects=False)


def queue_postal(ctx: UiContext) -> int:
    with ctx.open_tracker() as tracker:
        request = tracker.create("courtier-fictif", "postal", Jurisdiction.GDPR, NOW)
        tracker.apply(request.id, Action.QUEUE, NOW)
    return request.id


# --- Access control ----------------------------------------------------------------------


def test_pages_need_the_token(ctx: UiContext) -> None:
    anonymous = TestClient(create_app(ctx), base_url=BASE)

    assert anonymous.get("/").status_code == 403
    assert anonymous.get("/setup").status_code == 403
    assert anonymous.get("/login", params={"token": "wrong"}).status_code == 403
    assert "gomme ui" in anonymous.get("/").text


def test_login_sets_a_strict_http_only_cookie(ctx: UiContext) -> None:
    anonymous = TestClient(create_app(ctx), base_url=BASE)

    response = anonymous.get("/login", params={"token": ctx.token}, follow_redirects=False)

    cookie = response.headers["set-cookie"]
    assert f"{SESSION_COOKIE}=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie or "samesite=strict" in cookie.lower()


@pytest.mark.parametrize("host", ["evil.example:8765", "127.0.0.1:9999", "192.168.1.2:8765"])
def test_other_hosts_are_refused(client: TestClient, host: str) -> None:
    """Protection against DNS rebinding: only 127.0.0.1 and localhost on our port."""
    assert client.get("/", headers={"host": host}).status_code == 400


def test_post_needs_the_csrf_token(client: TestClient) -> None:
    response = client.post("/queue", data={"csrf": "forged"})

    assert response.status_code == 403
    assert client.post("/queue").status_code == 403
    assert client.post("/profile", data=IDENTITY).status_code == 403


def test_cross_site_origin_is_refused(client: TestClient, ctx: UiContext) -> None:
    response = client.post(
        "/queue", data={"csrf": ctx.csrf}, headers={"origin": "https://evil.example"}
    )

    assert response.status_code == 403


def test_forms_work_with_the_origin_a_browser_sends(client: TestClient, ctx: UiContext) -> None:
    """Browsers send the page's Origin with a form. With "Referrer-Policy: no-referrer"
    they would send "Origin: null" instead, and every button would be refused."""
    response = client.post(
        "/queue", data={"csrf": ctx.csrf}, headers={"origin": BASE}, follow_redirects=False
    )

    assert response.status_code == 303
    assert client.get("/").headers["referrer-policy"] == "same-origin"
    null = client.post("/queue", data={"csrf": ctx.csrf}, headers={"origin": "null"})
    assert null.status_code == 403
    assert null.headers["content-type"].startswith("text/plain; charset=utf-8")


def test_security_headers(client: TestClient) -> None:
    response = client.get("/")

    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"


def test_non_ascii_tokens_are_refused_not_crashing(ctx: UiContext, client: TestClient) -> None:
    anonymous = TestClient(create_app(ctx), base_url=BASE)

    assert anonymous.get("/login", params={"token": "é"}).status_code == 403
    assert client.post("/queue", data={"csrf": "é"}).status_code == 403


# --- First run: the wizard ------------------------------------------------------------------


@pytest.fixture
def first_run(store: Store) -> Store:
    store.config = None
    return store


def test_first_run_opens_the_wizard(client: TestClient, first_run: Store) -> None:
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/setup"
    assert "Reprenez la main" in client.get("/setup").text


def test_wizard_walkthrough_saves_the_profile(
    client: TestClient, ctx: UiContext, first_run: Store
) -> None:
    identity = post(client, ctx, "/setup", step="welcome", go="next")
    assert "Qui êtes-vous" in identity.text

    details = post(client, ctx, "/setup", step="identity", go="next", **IDENTITY)
    assert "retrouver votre fiche" in details.text
    assert "utile à 1 broker" in details.text  # courtier-fictif needs the address
    assert 'value="Jeanne"' in details.text  # carried over, hidden

    others = post(client, ctx, "/setup", step="details", go="next", **IDENTITY, **ADDRESS)
    assert "anciennes coordonnées" in others.text

    sending = post(
        client,
        ctx,
        "/setup",
        step="others",
        go="next",
        other_emails="ancienne@example.org",
        **IDENTITY,
        **ADDRESS,
    )
    assert "Comment envoyer vos demandes" in sending.text
    assert 'value="mail_app" checked' in sending.text  # not a Gmail address

    review = post(
        client, ctx, "/setup", step="sending", go="next", sending="gmail", **IDENTITY, **ADDRESS
    )
    assert "Tout est prêt" in review.text
    assert "Nulle-Part" in review.text
    assert first_run.config is None  # nothing written before the last step

    saved = post(
        client,
        ctx,
        "/setup",
        step="review",
        go="save",
        sending="gmail",
        other_emails="ancienne@example.org",
        **IDENTITY,
        **ADDRESS,
    )
    assert saved.status_code == 303
    assert saved.headers["location"].startswith("/?msg=")
    config = first_run.config
    assert config is not None
    assert config.profile.full_name == "Jeanne Exemple"
    assert config.profile.postal_address is not None
    assert config.email.composer is Composer.GMAIL
    assert config.email.mode is EmailMode.MANUAL
    assert config.profile.other_emails == ("ancienne@example.org",)
    assert "Préparer ma demande" in client.get("/").text


def test_wizard_suggests_gmail_for_a_gmail_address(
    client: TestClient, ctx: UiContext, first_run: Store
) -> None:
    gmail = {**IDENTITY, "email": "jeanne.exemple@gmail.com"}

    sending = post(client, ctx, "/setup", step="others", go="next", **gmail)

    assert 'value="gmail" checked' in sending.text
    assert 'value="smtp.gmail.com"' in sending.text  # ready if automatic sending is chosen
    assert "myaccount.google.com/apppasswords" in sending.text


def test_wizard_errors_keep_the_values(
    client: TestClient, ctx: UiContext, first_run: Store
) -> None:
    page = post(
        client, ctx, "/setup", step="identity", go="next", **{**IDENTITY, "email": "pas-un-email"}
    )

    assert "Qui êtes-vous" in page.text  # still on the same step
    assert "ne semble pas valide" in page.text
    assert 'value="Jeanne"' in page.text
    assert first_run.saved == 0


def test_wizard_incomplete_address(client: TestClient, ctx: UiContext, first_run: Store) -> None:
    page = post(client, ctx, "/setup", step="details", go="next", **IDENTITY, street="1 rue X")

    assert page.text.count("pour une adresse complète") == 2  # postal code and city


def test_wizard_back_keeps_the_values(client: TestClient, ctx: UiContext, first_run: Store) -> None:
    page = post(client, ctx, "/setup", step="details", go="back", **IDENTITY, **ADDRESS)

    assert "Qui êtes-vous" in page.text
    assert 'value="Jeanne"' in page.text
    assert 'value="Nulle-Part"' in page.text  # hidden, not lost


def test_wizard_save_checks_everything_again(
    client: TestClient, ctx: UiContext, first_run: Store
) -> None:
    page = post(client, ctx, "/setup", step="review", go="save", **{**IDENTITY, "first_name": ""})

    assert "Qui êtes-vous" in page.text
    assert "obligatoire" in page.text
    assert first_run.config is None


def test_wizard_is_closed_once_configured(client: TestClient, ctx: UiContext, store: Store) -> None:
    before = store.config

    assert client.get("/setup", follow_redirects=False).headers["location"] == "/"
    response = post(client, ctx, "/setup", step="review", go="save", **IDENTITY)

    assert response.headers["location"].startswith("/profile")
    assert store.config is before


def test_post_routes_without_config_go_to_the_wizard(
    client: TestClient, ctx: UiContext, first_run: Store
) -> None:
    for path in ("/queue", "/letters", "/monitor", "/brokers/courtier-fictif/queue", "/profile"):
        response = post(client, ctx, path)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/setup", path


def test_invalid_config_is_explained(ctx: UiContext) -> None:
    def broken() -> Config:
        raise ConfigError("config.yml invalide : profile.email")

    broken_ctx = UiContext(**{**ctx.__dict__, "load_config": broken, "config_exists": lambda: True})
    client = TestClient(create_app(broken_ctx), base_url=BASE)
    client.get("/login", params={"token": ctx.token})

    page = client.get("/").text
    assert "Configuration illisible" in page
    assert "profile.email" in page


# --- Profile ----------------------------------------------------------------------------------


def test_profile_is_masked(client: TestClient) -> None:
    page = client.get("/profile").text

    assert "J*** E***" in page
    assert "Exemple" not in page
    assert "/profile/edit" in page


def test_profile_edit_keeps_the_other_settings(
    client: TestClient, ctx: UiContext, store: Store, profile: Profile
) -> None:
    store.config = Config(
        profile=profile,
        email=EmailConfig(
            mode=EmailMode.SMTP, smtp=SmtpConfig(host="smtp.example.org", username="u")
        ),
        requests=RequestOptions(access=True),
    )
    form = client.get("/profile/edit").text
    assert 'value="Nulle-Part"' in form

    assert 'value="smtp" checked' in form

    response = post(
        client,
        ctx,
        "/profile",
        **IDENTITY,
        **{**ADDRESS, "city": "Ailleurs"},
        sending="smtp",
        smtp_host="smtp.example.org",
        smtp_port="465",
        smtp_security="ssl",
        smtp_username="u",
    )

    assert response.status_code == 303
    config = store.config
    assert config.profile.postal_address is not None
    assert config.profile.postal_address.city == "Ailleurs"
    assert config.email.mode is EmailMode.SMTP
    assert config.email.smtp is not None
    assert config.email.smtp.port == 465
    assert config.requests.access is True

    post(client, ctx, "/profile", **IDENTITY, **ADDRESS, sending="gmail")
    assert store.config.email.mode is EmailMode.MANUAL
    assert store.config.email.composer is Composer.GMAIL
    assert store.config.email.smtp is not None  # kept, to switch back later


def test_profile_edit_errors(client: TestClient, ctx: UiContext, store: Store) -> None:
    before = store.config

    page = post(client, ctx, "/profile", **IDENTITY, phone="appelez-moi", sending="mail_app")

    assert page.status_code == 200
    assert "Uniquement des chiffres" in page.text
    assert store.config is before


def test_profile_can_drop_the_address(client: TestClient, ctx: UiContext, store: Store) -> None:
    post(client, ctx, "/profile", **IDENTITY, sending="mail_app")

    assert store.config is not None
    assert store.config.profile.postal_address is None
    page = client.get("/").text
    assert "Des brokers attendent une information" in page
    assert "adresse postale" in page


# --- Dashboard and sending -----------------------------------------------------------------


def test_empty_dashboard(client: TestClient) -> None:
    page = client.get("/").text

    assert "Bonjour Jeanne" in page
    assert "Préparer ma demande" in page
    assert "<script" not in page


def test_queue_then_send_with_gmail(client: TestClient, ctx: UiContext, store: Store) -> None:
    assert store.config is not None
    store.config = store.config.model_copy(update={"email": EmailConfig(composer=Composer.GMAIL)})
    response = post(client, ctx, "/queue")
    assert "1%20demande" in response.headers["location"]
    assert "1 demande à envoyer" in client.get("/").text

    page = client.get("/send").text
    assert "Ouvrir dans Gmail" in page
    assert "https://mail.google.com/mail/?authuser=jeanne.exemple%40example.org" in page
    assert "to=dpo%40example.org" in page
    assert "<script" not in page

    sent = post(client, ctx, "/requests/1/mark", action="send", next="/send")
    assert sent.headers["location"].startswith("/send?msg=")
    with ctx.open_tracker() as tracker:
        assert tracker.get(1).status is Status.SENT
    assert "Rien à envoyer" in client.get("/send").text
    detail = client.get("/requests/1").text
    assert "26 nov. 2026" in detail  # legal deadline
    assert "encore 31 jours" in detail


def test_mail_app_uses_a_mailto_link(client: TestClient, ctx: UiContext) -> None:
    post(client, ctx, "/queue")

    page = client.get("/send").text

    assert "Ouvrir dans ma messagerie" in page
    assert 'href="mailto:dpo@example.org?subject=' in page


def test_eml_is_built_on_demand(client: TestClient, ctx: UiContext) -> None:
    post(client, ctx, "/queue")

    eml = client.get("/requests/1/eml")

    assert eml.status_code == 200
    assert eml.headers["content-type"].startswith("message/rfc822")
    assert "To: dpo@example.org" in eml.text
    assert not (ctx.outbox).exists()  # nothing written to disk
    post(client, ctx, "/requests/1/mark", action="send")
    assert client.get("/requests/1/eml").status_code == 404


def test_letters(client: TestClient, ctx: UiContext) -> None:
    request_id = queue_postal(ctx)
    assert "Préparer les courriers" in client.get("/send").text
    assert client.get(f"/requests/{request_id}/letter").status_code == 404

    response = post(client, ctx, "/letters")

    assert "1%20courrier" in response.headers["location"]
    pdf = client.get(f"/requests/{request_id}/letter")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert "Télécharger la lettre" in client.get("/send").text


def test_web_forms(client: TestClient, ctx: UiContext) -> None:
    post(client, ctx, "/brokers/fake-people-search/queue")

    page = client.get("/send").text

    assert "Ouvrir le formulaire" in page
    assert "https://example.org/fake-people-search/opt-out" in page
    assert "gomme submit --broker fake-people-search" in page


def test_follow_a_request(client: TestClient, ctx: UiContext) -> None:
    post(client, ctx, "/queue")
    post(client, ctx, "/requests/1/mark", action="send")

    page = client.get("/requests/1").text
    assert "Le broker a accusé réception" in page
    post(client, ctx, "/requests/1/mark", action="complete", note="email du 2 nov.")

    page = client.get("/requests/1").text
    assert "confirmé la suppression" in page
    assert "email du 2 nov." in page
    dashboard = client.get("/").text
    assert "Terminées" in dashboard
    assert "1 demande terminée sur 1" in dashboard


def test_next_cannot_leave_the_site(client: TestClient, ctx: UiContext) -> None:
    post(client, ctx, "/queue")

    response = post(client, ctx, "/requests/1/mark", action="send", next="https://evil.example/")

    assert response.headers["location"].startswith("/requests/1?msg=")


def test_reserved_actions_are_refused(client: TestClient, ctx: UiContext) -> None:
    post(client, ctx, "/queue")

    response = post(client, ctx, "/requests/1/mark", action="mark_overdue")

    assert "r%C3%A9serv%C3%A9e" in response.headers["location"]
    with ctx.open_tracker() as tracker:
        assert tracker.get(1).status is Status.QUEUED


def test_receipt_date_from_the_request_page(client: TestClient, ctx: UiContext) -> None:
    post(client, ctx, "/queue")
    post(client, ctx, "/requests/1/mark", action="send")

    assert "date de réception" in client.get("/requests/1").text
    response = post(client, ctx, "/requests/1/received", received_on="2026-10-26")
    assert "R%C3%A9ception%20not%C3%A9e" in response.headers["location"]
    bad = post(client, ctx, "/requests/1/received", received_on="hier")
    assert "invalide" in client.get(bad.headers["location"]).text


def test_monitor_button(client: TestClient, ctx: UiContext) -> None:
    response = post(client, ctx, "/monitor")

    assert "D%C3%A9lais%20v%C3%A9rifi%C3%A9s" in response.headers["location"]


# --- Brokers and odd inputs -----------------------------------------------------------------


def test_broker_pages_escape_their_content(client: TestClient) -> None:
    listing = client.get("/brokers", params={"q": "<script>alert(1)</script>"}).text
    assert "<script>alert(1)</script>" not in listing
    assert "&lt;script&gt;" in listing

    detail = client.get("/brokers/fake-people-search").text
    assert "5 étapes automatisées" in detail
    assert "gomme submit --broker fake-people-search" in detail


def test_queue_one_broker(client: TestClient, ctx: UiContext) -> None:
    response = post(client, ctx, "/brokers/fake-people-search/queue")

    assert "pr%C3%A9par%C3%A9e" in response.headers["location"]
    assert "voir le suivi" in client.get("/brokers/fake-people-search").text


def test_unknown_pages(client: TestClient) -> None:
    assert "Broker inconnu" in client.get("/brokers/inconnu").text
    assert "introuvable" in client.get("/requests/99").text
    assert client.get("/requests/99/eml").status_code == 404
    assert client.get("/requests/99/letter").status_code == 404


def test_huge_request_number(client: TestClient) -> None:
    assert "introuvable" in client.get(f"/requests/{2**70}").text


# --- Other identifiers ----------------------------------------------------------------------


def test_other_identifiers_are_saved_and_sent(
    client: TestClient, ctx: UiContext, store: Store
) -> None:
    post(
        client,
        ctx,
        "/profile",
        **IDENTITY,
        **ADDRESS,
        sending="mail_app",
        other_emails="ancienne@example.org\nJeanne.Exemple@example.org, autre@example.org",
        other_phones="06 00 00 00 01",
        prev_street_0="2 avenue de l'Ancien",
        prev_postal_code_0="00001",
        prev_city_0="Autrefois",
    )

    assert store.config is not None
    profile = store.config.profile
    assert profile.other_emails == ("ancienne@example.org", "autre@example.org")  # main dropped
    assert profile.other_phones == ("06 00 00 00 01",)
    assert [a.city for a in profile.previous_addresses] == ["Autrefois"]
    assert "Anciennes adresses" in client.get("/profile").text

    post(client, ctx, "/queue")
    body = client.get("/send").text
    assert "ancienne@example.org" in body  # courtier-fictif identifies by email
    assert "Autrefois" in body  # and by postal address
    assert "06 00 00 00 01" not in body  # but not by phone: minimisation


def test_other_identifiers_errors(client: TestClient, ctx: UiContext, store: Store) -> None:
    before = store.config

    page = post(
        client,
        ctx,
        "/profile",
        **IDENTITY,
        sending="mail_app",
        other_emails="ancienne@example.org\npas-un-email",
        prev_street_0="2 avenue",
    )

    assert "La 2e adresse de la liste ne semble pas valide" in page.text
    assert "pas-un-email" in page.text  # kept in the field, not in the message
    assert page.text.count("pour une adresse complète") == 2
    assert store.config is before


# --- Automatic sending -----------------------------------------------------------------------


@pytest.fixture
def smtp_store(store: Store, profile: Profile) -> Store:
    store.config = Config(
        profile=profile,
        email=EmailConfig(
            mode=EmailMode.SMTP,
            smtp=SmtpConfig(host="smtp.example.org", username="jeanne.exemple@example.org"),
            delay_seconds=0,
        ),
    )
    return store


def test_automatic_sending(
    client: TestClient,
    ctx: UiContext,
    smtp_store: Store,
    passwords: Passwords,
    fake_smtp: FakeSmtp,
) -> None:
    post(client, ctx, "/queue")
    page = client.get("/send").text
    assert "Tout envoyer automatiquement" in page
    assert 'name="password"' in page

    response = post(
        client, ctx, "/send/auto", password="abcd efgh ijkl mnop", remember="yes", confirm="yes"
    )

    assert response.headers["location"] == "/send/progress"
    assert len(fake_smtp.sent) == 1
    assert fake_smtp.password == "abcdefghijklmnop"  # spaces of Gmail's display removed
    with ctx.open_tracker() as tracker:
        assert tracker.get(1).status is Status.SENT
    progress = client.get("/send/progress").text
    assert "1 demande envoyée" in progress
    assert "trousseau" in progress
    assert passwords.saved == {"jeanne.exemple@example.org": "abcdefghijklmnop"}
    assert "abcdefghijklmnop" not in progress
    assert 'name="password"' not in client.get("/send").text  # known now


def test_automatic_sending_needs_confirmation(
    client: TestClient, ctx: UiContext, smtp_store: Store, fake_smtp: FakeSmtp
) -> None:
    post(client, ctx, "/queue")

    response = post(client, ctx, "/send/auto", password="secret")

    assert "Cochez" in client.get(response.headers["location"]).text
    assert fake_smtp.sent == []


def test_wrong_password_is_explained_and_not_kept(
    client: TestClient,
    ctx: UiContext,
    smtp_store: Store,
    passwords: Passwords,
    fake_smtp: FakeSmtp,
) -> None:
    fake_smtp.reject_login = True
    post(client, ctx, "/queue")

    post(client, ctx, "/send/auto", password="wrong", remember="yes", confirm="yes")

    progress = client.get("/send/progress").text
    assert "pas pu commencer" in progress
    assert "mot de passe d&#39;application" in progress
    assert passwords.saved == {}
    with ctx.open_tracker() as tracker:
        assert tracker.get(1).status is Status.QUEUED


def test_forget_the_saved_password(
    client: TestClient, ctx: UiContext, smtp_store: Store, passwords: Passwords
) -> None:
    passwords.saved["jeanne.exemple@example.org"] = "secret"
    page = client.get("/profile").text
    assert "Oublier ce mot de passe" in page
    assert "jeanne.exemple@example.org" not in page  # masked

    post(client, ctx, "/smtp/forget")

    assert passwords.saved == {}


def test_automatic_sending_needs_smtp_mode(client: TestClient, ctx: UiContext) -> None:
    post(client, ctx, "/queue")

    response = post(client, ctx, "/send/auto", password="x", confirm="yes")

    assert "pas%20activ%C3%A9" in response.headers["location"]
    assert "Tout envoyer automatiquement" not in client.get("/send").text


SMTP_FORM = {
    "sending": "smtp",
    "smtp_host": "smtp.example.org",
    "smtp_port": "587",
    "smtp_security": "starttls",
    "smtp_username": "jeanne.exemple@example.org",
}


def test_password_typed_in_the_profile_is_checked_and_kept(
    client: TestClient,
    ctx: UiContext,
    store: Store,
    passwords: Passwords,
    fake_smtp: FakeSmtp,
) -> None:
    response = post(
        client, ctx, "/profile", **IDENTITY, **SMTP_FORM, smtp_password="abcd efgh ijkl mnop"
    )

    assert "v%C3%A9rifi%C3%A9" in response.headers["location"]
    assert passwords.saved == {"jeanne.exemple@example.org": "abcdefghijklmnop"}
    assert fake_smtp.sent == []  # a login only, no email
    assert "login:jeanne.exemple@example.org" in fake_smtp.calls
    assert store.config is not None
    assert store.config.email.mode is EmailMode.SMTP
    form = client.get("/profile/edit").text
    assert "déjà enregistré" in form
    assert "abcdefghijklmnop" not in form  # never shown again
    assert 'name="password"' not in client.get("/send").text


def test_wrong_password_in_the_profile_is_refused(
    client: TestClient,
    ctx: UiContext,
    store: Store,
    passwords: Passwords,
    fake_smtp: FakeSmtp,
) -> None:
    fake_smtp.reject_login = True
    before = store.config

    page = post(client, ctx, "/profile", **IDENTITY, **SMTP_FORM, smtp_password="wrong")

    assert "mot de passe d&#39;application" in page.text
    assert "wrong" not in page.text
    assert passwords.saved == {}
    assert store.config is before


def test_wizard_keeps_the_typed_password(
    client: TestClient, ctx: UiContext, first_run: Store, passwords: Passwords
) -> None:
    review = post(
        client,
        ctx,
        "/setup",
        step="sending",
        go="next",
        smtp_password="secret",
        **IDENTITY,
        **SMTP_FORM,
    )

    assert "Tout est prêt" in review.text
    assert "mot de passe vérifié et enregistré" in review.text
    assert "secret" not in review.text  # not carried in hidden fields
    assert passwords.saved == {"jeanne.exemple@example.org": "secret"}
