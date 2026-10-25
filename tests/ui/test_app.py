"""Tests for the local web interface: access control first, then the pages."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from gomme.brokers.registry import load_registry
from gomme.config import Config, ConfigError
from gomme.profile import Profile
from gomme.tracker.db import Tracker
from gomme.tracker.state import Status
from gomme.ui.app import SESSION_COOKIE, UiContext, create_app

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


@pytest.fixture
def ctx(tmp_path: Path, profile: Profile, fixtures_brokers_dir: Path) -> UiContext:
    brokers = load_registry(fixtures_brokers_dir)
    db_path = tmp_path / "gomme.db"

    @contextmanager
    def open_tracker() -> Iterator[Tracker]:
        with Tracker(db_path) as tracker:
            yield tracker

    return UiContext(
        load_config=lambda: Config(profile=profile),
        load_brokers=lambda: brokers,
        open_tracker=open_tracker,
        clock=lambda: NOW,
        outbox=tmp_path / "outbox",
        token="test-token-not-secret",
        port=PORT,
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


# --- Access control ----------------------------------------------------------------------


def test_pages_need_the_token(ctx: UiContext) -> None:
    anonymous = TestClient(create_app(ctx), base_url=BASE)

    assert anonymous.get("/").status_code == 403
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


def test_cross_site_origin_is_refused(client: TestClient, ctx: UiContext) -> None:
    response = client.post(
        "/queue", data={"csrf": ctx.csrf}, headers={"origin": "https://evil.example"}
    )

    assert response.status_code == 403


def test_security_headers(client: TestClient) -> None:
    response = client.get("/")

    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    assert "<script" not in response.text


# --- Pages and actions --------------------------------------------------------------------


def test_empty_dashboard(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "Aucune demande" in response.text


def test_queue_then_follow_a_request(client: TestClient, ctx: UiContext) -> None:
    response = post(client, ctx, "/queue")
    assert response.status_code == 303
    assert "1%20demande" in response.headers["location"]

    dashboard = client.get("/").text
    assert "courtier-fictif" in dashboard
    assert "en attente d&#39;envoi" in dashboard

    response = post(client, ctx, "/requests/1/mark", action="send", note="envoyé à la main")
    assert response.status_code == 303
    page = client.get("/requests/1").text
    assert "envoyé à la main" in page
    assert "2026-11-26" in page  # legal deadline


def test_reserved_actions_are_refused(client: TestClient, ctx: UiContext) -> None:
    post(client, ctx, "/queue")

    response = post(client, ctx, "/requests/1/mark", action="mark_overdue")

    assert "r%C3%A9serv%C3%A9e" in response.headers["location"]
    with ctx.open_tracker() as tracker:
        assert tracker.get(1).status is Status.QUEUED


def test_broker_pages_escape_their_content(client: TestClient) -> None:
    listing = client.get("/brokers", params={"q": "<script>alert(1)</script>"}).text
    assert "<script>alert(1)</script>" not in listing
    assert "&lt;script&gt;" in listing

    detail = client.get("/brokers/fake-people-search").text
    assert "5 étapes automatisées" in detail
    assert "gomme submit --broker fake-people-search" in detail


def test_queue_one_broker(client: TestClient, ctx: UiContext) -> None:
    response = post(client, ctx, "/brokers/fake-people-search/queue")

    assert "file" in response.headers["location"]
    assert "Dernière demande" in client.get("/brokers/fake-people-search").text


def test_send_preview_and_eml_files_in_manual_mode(client: TestClient, ctx: UiContext) -> None:
    post(client, ctx, "/brokers/courtier-fictif/queue")

    preview = client.get("/send").text
    assert "dpo@example.org" in preview
    assert "Préparer les fichiers .eml" in preview

    post(client, ctx, "/send")
    eml = client.get("/requests/1/eml")
    assert eml.status_code == 200
    assert eml.headers["content-type"].startswith("message/rfc822")
    assert "To: dpo@example.org" in eml.text


def test_smtp_mode_sends_nothing_from_the_browser(
    tmp_path: Path, profile: Profile, ctx: UiContext
) -> None:
    from gomme.config import EmailConfig, EmailMode, SmtpConfig

    smtp = Config(
        profile=profile,
        email=EmailConfig(
            mode=EmailMode.SMTP, smtp=SmtpConfig(host="smtp.example.org", username="u")
        ),
    )
    smtp_ctx = UiContext(**{**ctx.__dict__, "load_config": lambda: smtp})
    client = TestClient(create_app(smtp_ctx), base_url=BASE)
    client.get("/login", params={"token": ctx.token})
    post(client, smtp_ctx, "/brokers/courtier-fictif/queue")

    response = post(client, smtp_ctx, "/send")

    assert "gomme%20send" in response.headers["location"]
    with ctx.open_tracker() as tracker:
        assert tracker.get(1).status is Status.QUEUED


def test_profile_is_masked(client: TestClient) -> None:
    page = client.get("/profile").text

    assert "J*** E***" in page
    assert "Exemple" not in page


def test_missing_config_is_explained(ctx: UiContext) -> None:
    def no_config() -> Config:
        raise ConfigError("aucune configuration : lancer « gomme init »")

    broken = UiContext(**{**ctx.__dict__, "load_config": no_config})
    client = TestClient(create_app(broken), base_url=BASE)
    client.get("/login", params={"token": ctx.token})

    assert "gomme init" in client.get("/profile").text


def test_unknown_pages(client: TestClient) -> None:
    assert "Broker inconnu" in client.get("/brokers/inconnu").text
    assert "introuvable" in client.get("/requests/99").text
    assert client.get("/requests/99/eml").status_code == 404


def test_monitor_button(client: TestClient, ctx: UiContext) -> None:
    response = post(client, ctx, "/monitor")

    assert (
        "Suivi" in response.headers["location"]
        or "Suivi" in client.get(response.headers["location"]).text
    )


def test_receipt_date_from_the_request_page(client: TestClient, ctx: UiContext) -> None:
    post(client, ctx, "/brokers/courtier-fictif/queue")
    post(client, ctx, "/requests/1/mark", action="send")

    assert "Noter la réception" in client.get("/requests/1").text
    response = post(client, ctx, "/requests/1/received", received_on="2026-10-26")
    assert "R%C3%A9ception%20not%C3%A9e" in response.headers["location"]
    bad = post(client, ctx, "/requests/1/received", received_on="hier")
    assert "invalide" in client.get(bad.headers["location"]).text


def test_post_routes_survive_a_missing_config(ctx: UiContext) -> None:
    def no_config() -> Config:
        raise ConfigError("absente")

    broken = UiContext(**{**ctx.__dict__, "load_config": no_config})
    client = TestClient(create_app(broken), base_url=BASE)
    client.get("/login", params={"token": ctx.token})

    for path in ("/queue", "/send", "/monitor", "/brokers/courtier-fictif/queue"):
        response = post(client, broken, path)
        assert response.status_code == 303, path
        assert "gomme%20init" in response.headers["location"]


def test_non_ascii_tokens_are_refused_not_crashing(ctx: UiContext, client: TestClient) -> None:
    anonymous = TestClient(create_app(ctx), base_url=BASE)

    assert anonymous.get("/login", params={"token": "é"}).status_code == 403
    assert client.post("/queue", data={"csrf": "é"}).status_code == 403


def test_huge_request_number(client: TestClient) -> None:
    assert "introuvable" in client.get(f"/requests/{2**70}").text
