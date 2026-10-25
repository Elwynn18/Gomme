"""Tests for the command-line interface."""

from importlib.metadata import version
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gomme.cli import app

# A wide terminal keeps Rich from wrapping table cells.
runner = CliRunner(env={"COLUMNS": "200", "NO_COLOR": "1"})

FR_SLUG = "courtier-fictif"
US_SLUG = "fake-people-search"


def test_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == f"gomme {version('gomme')}"


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])

    assert "brokers" in result.output


def test_brokers_list_with_option(fixtures_brokers_dir: Path) -> None:
    result = runner.invoke(app, ["brokers", "list", "--brokers-dir", str(fixtures_brokers_dir)])

    assert result.exit_code == 0
    assert FR_SLUG in result.stdout
    assert US_SLUG in result.stdout


def test_brokers_list_with_environment_variable(fixtures_brokers_dir: Path) -> None:
    result = runner.invoke(
        app, ["brokers", "list"], env={"GOMME_BROKERS_DIR": str(fixtures_brokers_dir)}
    )

    assert result.exit_code == 0
    assert FR_SLUG in result.stdout
    assert US_SLUG in result.stdout


@pytest.mark.parametrize(
    ("region", "shown", "hidden"), [("fr", FR_SLUG, US_SLUG), ("us", US_SLUG, FR_SLUG)]
)
def test_brokers_list_filters_by_region(
    fixtures_brokers_dir: Path, region: str, shown: str, hidden: str
) -> None:
    result = runner.invoke(
        app, ["brokers", "list", "--region", region, "--brokers-dir", str(fixtures_brokers_dir)]
    )

    assert result.exit_code == 0
    assert shown in result.stdout
    assert hidden not in result.stdout


def test_brokers_list_rejects_unknown_region(fixtures_brokers_dir: Path) -> None:
    result = runner.invoke(
        app, ["brokers", "list", "--region", "de", "--brokers-dir", str(fixtures_brokers_dir)]
    )

    assert result.exit_code != 0


def test_brokers_list_with_no_broker_in_region(fixtures_brokers_dir: Path) -> None:
    result = runner.invoke(
        app, ["brokers", "list", "--region", "eu", "--brokers-dir", str(fixtures_brokers_dir)]
    )

    assert result.exit_code == 0
    assert "Aucune fiche" in result.stdout


def test_brokers_list_defaults_to_packaged_registry() -> None:
    result = runner.invoke(app, ["brokers", "list"])

    assert result.exit_code == 0
    assert "fiche(s)" in result.stdout


def test_brokers_list_reports_invalid_registry(tmp_path: Path) -> None:
    broken = tmp_path / "fr" / "courtier-fictif.yml"
    broken.parent.mkdir()
    broken.write_text("slug: [\n", encoding="utf-8")

    result = runner.invoke(app, ["brokers", "list", "--brokers-dir", str(tmp_path)])

    assert result.exit_code == 1
    assert "courtier-fictif.yml" in result.stderr
    assert result.stdout == ""


def test_brokers_list_reports_missing_directory(tmp_path: Path) -> None:
    result = runner.invoke(app, ["brokers", "list", "--brokers-dir", str(tmp_path / "absent")])

    assert result.exit_code == 1
    assert "absent" in result.stderr


def test_brokers_show(fixtures_brokers_dir: Path) -> None:
    result = runner.invoke(
        app, ["brokers", "show", US_SLUG, "--brokers-dir", str(fixtures_brokers_dir)]
    )

    assert result.exit_code == 0, result.output
    assert "5 étape(s) automatisée(s)" in result.output
    assert "privacy@example.org" in result.output
    assert "full_name, email" in result.output
    assert "FAKE: test fixture" in result.output


def test_brokers_show_unknown(fixtures_brokers_dir: Path) -> None:
    result = runner.invoke(
        app, ["brokers", "show", "inconnu", "--brokers-dir", str(fixtures_brokers_dir)]
    )

    assert result.exit_code == 1
    assert "broker inconnu" in result.output
