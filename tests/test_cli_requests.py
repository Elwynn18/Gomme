"""Tests for the request commands: init, profile, queue, send, status, mark, history, monitor."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from fakes import FakePage
from typer.testing import CliRunner

from gomme import cli
from gomme.cli import app
from gomme.config import (
    Config,
    EmailConfig,
    EmailMode,
    SmtpConfig,
    config_path,
    data_dir,
    save_config,
)
from gomme.profile import PostalAddress, Profile

runner = CliRunner(env={"COLUMNS": "200", "NO_COLOR": "1"})

INIT_ANSWERS = "\n".join(
    [
        "Jeanne",
        "Exemple",
        "jeanne.exemple@example.org",
        "FR",
        "fr",
        "y",  # postal address
        "1 rue de l'Exemple",
        "00000",
        "Nulle-Part",
        "",  # no birth date
        "",  # no phone
        "n",  # manual email mode
        "",
    ]
)


@pytest.fixture(autouse=True)
def _fixtures_and_clock(monkeypatch: pytest.MonkeyPatch, fixtures_brokers_dir: Path) -> None:
    monkeypatch.setenv("GOMME_BROKERS_DIR", str(fixtures_brokers_dir))
    monkeypatch.setattr(cli, "now", lambda: datetime(2026, 10, 26, 9, 0, tzinfo=UTC))


@pytest.fixture
def configured(profile: Profile) -> Config:
    config = Config(profile=profile)
    save_config(config)
    return config


def invoke(*args: str, input: str | None = None) -> tuple[int, str]:
    result = runner.invoke(app, list(args), input=input)
    return result.exit_code, result.output


# --- init / profile ---------------------------------------------------------------------


def test_init_writes_a_private_config() -> None:
    code, output = invoke("init", input=INIT_ANSWERS)

    assert code == 0, output
    data = yaml.safe_load(config_path().read_text(encoding="utf-8"))
    assert data["profile"]["first_name"] == "Jeanne"
    assert data["profile"]["postal_address"]["city"] == "Nulle-Part"
    assert "birth_date" not in data["profile"]
    assert (config_path().stat().st_mode & 0o777) == 0o600


def test_init_refuses_to_overwrite(configured: Config) -> None:
    code, output = invoke("init", input=INIT_ANSWERS)

    assert code == 1
    assert "--force" in output


def test_init_rejects_an_invalid_profile() -> None:
    answers = INIT_ANSWERS.replace("jeanne.exemple@example.org", "pas-une-adresse")

    code, output = invoke("init", input=answers)

    assert code == 1
    assert "profil invalide" in output
    assert not config_path().exists()


def test_profile_is_masked(configured: Config) -> None:
    code, output = invoke("profile")

    assert code == 0
    assert "J*** E***" in output
    assert "Exemple" not in output
    assert "example.org" not in output.replace("e***.org", "")


def test_commands_need_a_config() -> None:
    code, output = invoke("queue")

    assert code == 1
    assert "gomme init" in output


def test_world_readable_config_is_flagged(configured: Config) -> None:
    config_path().chmod(0o644)

    _, output = invoke("profile")

    assert "chmod 600" in output


# --- queue / send / status / mark / history ----------------------------------------------


def test_queue_defaults_to_the_regions_of_the_user(configured: Config) -> None:
    code, output = invoke("queue")

    assert code == 0, output
    assert "Sélection : fiches France (fr) et internationales (global)" in output
    assert "1 demande(s) mise(s) en file" in output


def test_queue_rejects_unknown_brokers(configured: Config) -> None:
    code, output = invoke("queue", "--broker", "inconnu")

    assert code == 1
    assert "inconnu" in output


def test_full_manual_cycle(configured: Config) -> None:
    assert invoke("queue", "--broker", "courtier-fictif")[0] == 0

    code, output = invoke("send", "--dry-run")
    assert code == 0, output
    assert "Simulation : 1 email(s)" in output
    assert "dpo@example.org" in output
    assert "jeanne" not in output.lower()  # no body unless --show

    code, output = invoke("send", "--dry-run", "--show")
    assert "Pour me retrouver dans vos fichiers" in output

    code, output = invoke("send")
    assert code == 0, output
    assert "1-courtier-fictif.eml" in output.replace("\n", "")
    assert "gomme mark <n°> send" in output

    code, output = invoke("mark", "1", "send", "--note", "envoyé à la main")
    assert (code, output.strip()) == (0, "Demande n° 1 : envoyée")

    code, output = invoke("status")
    assert code == 0
    assert "courtier-fictif" in output
    assert "2026-11-26 (+31 j)" in output

    code, output = invoke("history", "1")
    assert "envoyé à la main" in output
    assert [line.split()[2] for line in output.splitlines()[1:]] == ["create", "queue", "send"]


def test_mark_refuses_unknown_and_impossible_actions(configured: Config) -> None:
    invoke("queue", "--broker", "courtier-fictif")

    code, output = invoke("mark", "1", "frobnicate")
    assert code == 1
    assert "action inconnue" in output

    code, output = invoke("mark", "1", "complete")
    assert code == 1
    assert "impossible" in output

    code, output = invoke("mark", "1", "mark_overdue")  # reserved to `monitor`
    assert code == 1

    code, output = invoke("mark", "99", "send")
    assert code == 1
    assert "introuvable" in output


def test_status_without_requests(configured: Config) -> None:
    code, output = invoke("status")

    assert code == 0
    assert "gomme queue" in output


def test_monitor_marks_overdue(configured: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    invoke("queue", "--broker", "courtier-fictif")
    invoke("mark", "1", "send")
    monkeypatch.setattr(cli, "now", lambda: datetime(2026, 12, 1, tzinfo=UTC))

    code, output = invoke("monitor")

    assert code == 0
    assert "courtier-fictif : en retard" in output
    assert "en retard" in invoke("status")[1]


# --- SMTP mode safety ------------------------------------------------------------------------


@pytest.fixture
def smtp_configured(profile: Profile) -> Config:
    config = Config(
        profile=profile,
        email=EmailConfig(
            mode=EmailMode.SMTP, smtp=SmtpConfig(host="smtp.example.org", username="jeanne")
        ),
    )
    save_config(config)
    return config


def test_smtp_send_needs_the_password_variable(smtp_configured: Config) -> None:
    invoke("queue", "--broker", "courtier-fictif")

    code, output = invoke("send")

    assert code == 1
    assert "GOMME_SMTP_PASSWORD" in output


def test_smtp_send_asks_for_confirmation(
    smtp_configured: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOMME_SMTP_PASSWORD", "fictional-password")
    invoke("queue", "--broker", "courtier-fictif")

    code, output = invoke("send", input="n\n")

    assert code == 1
    assert "Envoyer jusqu'à 1 email(s) depuis j***@e***.org" in output


def test_smtp_dry_run_needs_no_password(smtp_configured: Config) -> None:
    invoke("queue", "--broker", "courtier-fictif")

    code, output = invoke("send", "--dry-run")

    assert code == 0, output
    assert "rien n'est envoyé" in output


# --- escalate ------------------------------------------------------------------------------


def test_escalate_writes_a_private_draft(
    configured: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    invoke("queue", "--broker", "courtier-fictif")
    invoke("mark", "1", "send")
    monkeypatch.setattr(cli, "now", lambda: datetime(2026, 12, 1, tzinfo=UTC))
    invoke("monitor")

    code, output = invoke("escalate", "1")

    assert code == 0, output
    path = data_dir() / "complaints" / "1-courtier-fictif.txt"
    assert (path.stat().st_mode & 0o777) == 0o600
    assert "BROUILLON DE PLAINTE" in path.read_text(encoding="utf-8")
    assert "Gomme ne l'envoie pas" in output
    assert "plainte préparée" in invoke("status")[1]


def test_escalate_a_refusal_asks_whether_it_was_contested(configured: Config) -> None:
    invoke("queue", "--broker", "courtier-fictif")
    invoke("mark", "1", "send")
    invoke("mark", "1", "refuse")

    code, output = invoke("escalate", "1", input="n\n")

    assert code == 1
    assert "contesté ce refus" in output
    assert invoke("escalate", "1", "--yes")[0] == 0


def test_escalate_refuses_requests_still_running(configured: Config) -> None:
    invoke("queue", "--broker", "courtier-fictif")
    invoke("mark", "1", "send")

    code, output = invoke("escalate", "1")

    assert code == 1
    assert "en retard ou refusée" in output


# --- letters ---------------------------------------------------------------------------------


def test_letters_for_postal_requests(configured: Config) -> None:
    invoke("queue", "--broker", "courtier-fictif", "--channel", "postal")

    code, output = invoke("letters")

    assert code == 0, output
    path = data_dir() / "letters" / "1-courtier-fictif.pdf"
    assert path.read_bytes().startswith(b"%PDF-")
    assert (path.stat().st_mode & 0o777) == 0o600
    assert "recommandé" in output
    assert invoke("send", "--dry-run")[1].count("Simulation : 0 email(s)") == 1
    assert invoke("mark", "1", "send")[0] == 0


# --- submit / verify (browser replaced by a fake page) -----------------------------------------


@pytest.fixture
def fake_browser(monkeypatch: pytest.MonkeyPatch) -> FakePage:
    page = FakePage({"input[name='name']", "input[name='email']", ".captcha"})

    @contextmanager
    def open_browser(_profile_dir: Path, *, headless: bool = False) -> Iterator[FakePage]:
        yield page

    monkeypatch.setattr(cli, "open_browser", open_browser)
    monkeypatch.setattr(cli.webbrowser, "open", lambda _url: True)
    return page


def test_submit_fills_the_form_and_records_the_proof(
    configured: Config, fake_browser: FakePage
) -> None:
    invoke("queue", "--broker", "fake-people-search")

    # Enter after the CAPTCHA pause, then "y" to confirm the submission.
    code, output = invoke("submit", input="\ny\n")

    assert code == 0, output
    assert ("fill", "input[name='name']", "Jeanne Exemple", "fill", "None") in fake_browser.log
    assert "Résolvez le CAPTCHA" in output
    assert "1 formulaire(s) envoyé(s)" in output
    assert "envoyée" in invoke("status")[1]


def test_verify_reports_a_working_form(fake_browser: FakePage) -> None:
    code, output = invoke("verify", "--broker", "fake-people-search")

    assert code == 0, output
    assert "✓ fill_form.name" in output
    assert "arrêtée à l'étape solve_captcha" in output


def test_verify_reports_a_changed_form(fake_browser: FakePage) -> None:
    fake_browser.present.clear()

    code, output = invoke("verify", "--broker", "fake-people-search")

    assert code == 1
    assert "✗ fill_form.name" in output
    assert "mettre à jour la fiche" in output


def test_verify_needs_an_automated_form() -> None:
    code, output = invoke("verify", "--broker", "courtier-fictif")

    assert code == 1
    assert "pas de formulaire automatisé" in output


def test_remind_an_overdue_request(configured: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    invoke("queue", "--broker", "courtier-fictif")
    invoke("mark", "1", "send")
    assert invoke("remind", "1")[0] == 1  # not overdue yet
    monkeypatch.setattr(cli, "now", lambda: datetime(2026, 12, 1, tzinfo=UTC))
    invoke("monitor")

    code, output = invoke("remind", "1", "--dry-run")
    assert code == 0, output
    assert "Relance" in output
    assert not (data_dir() / "outbox" / "1-courtier-fictif-relance-1.eml").exists()

    code, output = invoke("remind", "1")
    assert code == 0, output
    assert (data_dir() / "outbox" / "1-courtier-fictif-relance-1.eml").exists()
    assert "relance préparée" in invoke("history", "1")[1]


def test_californians_are_pointed_to_drop(profile: Profile) -> None:
    address = PostalAddress(
        street="1 Example St", postal_code="00000", city="Nowhere", country="US", state="CA"
    )
    save_config(
        Config(
            profile=profile.model_copy(
                update={"country": "US", "language": "en", "postal_address": address}
            )
        )
    )

    _, output = invoke("queue")

    assert "privacy.ca.gov/drop" in output


def test_mark_send_with_the_real_date(configured: Config) -> None:
    invoke("queue", "--broker", "courtier-fictif")

    code, output = invoke("mark", "1", "send", "--date", "2026-10-19")

    assert code == 0, output
    assert "2026-10-19" in invoke("status")[1]
    assert invoke("mark", "1", "acknowledge", "--date", "2026-10-19")[0] == 1


def test_mark_send_with_the_receipt_date_of_a_letter(configured: Config) -> None:
    invoke("queue", "--broker", "courtier-fictif", "--channel", "postal")

    code, output = invoke("mark", "1", "send", "--date", "2026-10-19", "--received", "2026-10-21")

    assert code == 0, output
    assert "2026-11-23" in invoke("status")[1]  # 21 November is a Saturday


def test_init_errors_never_echo_what_was_typed() -> None:
    answers = INIT_ANSWERS.replace("1 rue de l'Exemple", "1 rue X\x07secret")

    code, output = invoke("init", input=answers)

    assert code == 1
    error = output.split("Erreur :", 1)[1]  # before it: the terminal echo of the answers
    assert "postal_address.street" in error
    assert "secret" not in error


def test_init_asks_again_for_invalid_smtp_answers() -> None:
    answers = INIT_ANSWERS.rsplit("n\n", 1)[0] + "\n".join(
        ["y", "tls", "ssl", "smtp.example.org", "abc", "465", "jeanne", ""]
    )

    code, output = invoke("init", input=answers)

    assert code == 0, output
    assert "Valeurs possibles : starttls, ssl" in output
    assert "entre 1 et 65535" in output
    data = yaml.safe_load(config_path().read_text(encoding="utf-8"))
    assert data["email"]["smtp"]["port"] == 465
    assert "password" not in data["email"]["smtp"]


def test_requeue_with_another_channel(configured: Config) -> None:
    invoke("queue", "--broker", "courtier-fictif")
    invoke("mark", "1", "unqueue")

    code, output = invoke("queue", "--broker", "courtier-fictif", "--channel", "postal")

    assert code == 0, output
    assert "postal" in invoke("status")[1]
    assert "canal email → postal" in invoke("history", "1")[1]


def test_mark_several_requests_at_once(configured: Config) -> None:
    for slug in ("courtier-fictif", "fake-people-search"):
        invoke("queue", "--broker", slug)

    code, output = invoke("mark", "1-2", "send")

    assert code == 0, output
    assert output.count(": envoyée") == 2


def test_mark_reports_each_failure_and_goes_on(configured: Config) -> None:
    invoke("queue", "--broker", "courtier-fictif")

    code, output = invoke("mark", "7,1", "send")

    assert code == 1
    assert "n° 7 : demande n° 7 introuvable" in output
    assert "Demande n° 1 : envoyée" in output


@pytest.mark.parametrize("numbers", ["a", "3-1", "1-", "1-5000"])
def test_mark_rejects_bad_numbers(configured: Config, numbers: str) -> None:
    code, output = invoke("mark", numbers, "send")

    assert code == 1
    assert "invalide" in output


def test_received_command(configured: Config) -> None:
    invoke("queue", "--broker", "courtier-fictif", "--channel", "postal")
    invoke("mark", "1", "send", "--date", "2026-10-19")

    code, output = invoke("received", "1", "2026-10-21")

    assert code == 0, output
    assert "échéance le 2026-11-23" in output
    assert invoke("received", "1", "2026-10-01")[0] == 1  # before sending
