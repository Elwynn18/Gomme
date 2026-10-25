"""Tests for loading broker definitions from a directory tree."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from gomme.brokers.registry import (
    BrokerFileError,
    RegistryError,
    default_brokers_dir,
    load_registry,
)
from gomme.brokers.schema import Region


def broker_data(
    slug: str = "courtier-fictif", region: str = "fr", **overrides: Any
) -> dict[str, Any]:
    """A valid, entirely fictional broker definition."""
    data: dict[str, Any] = {
        "slug": slug,
        "name": "Courtier Fictif",
        "domain": "example.org",
        "country": "FR",
        "region": region,
        "category": "marketing",
        "channels": [{"type": "email", "address": "dpo@example.org"}],
        "legal_basis": ["gdpr_art17", "gdpr_art21"],
        "required_fields": ["full_name", "email"],
        "requires_id": False,
        "language": "fr",
        "source": "manual",
        "source_ref": "https://example.org/source",
        "last_verified": "2026-10-24",
    }
    data.update(overrides)
    return data


def write(root: Path, relative_path: str, content: str | dict[str, Any]) -> Path:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    text = content if isinstance(content, str) else yaml.safe_dump(content, sort_keys=False)
    path.write_text(text, encoding="utf-8")
    return path


def load_errors(root: Path) -> tuple[BrokerFileError, ...]:
    with pytest.raises(RegistryError) as exc_info:
        load_registry(root)
    return exc_info.value.errors


# --- Successful loads ---------------------------------------------------------


def test_loads_fixture_brokers(fixtures_brokers_dir: Path) -> None:
    brokers = load_registry(fixtures_brokers_dir)

    assert [broker.slug for broker in brokers] == ["courtier-fictif", "fake-people-search"]


def test_brokers_are_ordered_by_region_then_slug(tmp_path: Path) -> None:
    write(tmp_path, "us/b-us.yml", broker_data("b-us", "us"))
    write(tmp_path, "global/a-global.yml", broker_data("a-global", "global"))
    write(tmp_path, "fr/z-fr.yml", broker_data("z-fr", "fr"))
    write(tmp_path, "fr/a-fr.yml", broker_data("a-fr", "fr"))
    write(tmp_path, "eu/m-eu.yml", broker_data("m-eu", "eu"))

    brokers = load_registry(tmp_path)

    assert [broker.slug for broker in brokers] == ["a-fr", "z-fr", "m-eu", "b-us", "a-global"]


def test_packaged_registry_loads() -> None:
    """Every definition shipped with Gomme is valid."""
    root = default_brokers_dir()

    assert {path.name for path in root.iterdir()} == {region.value for region in Region}
    assert len(load_registry(root)) > 0


def test_empty_root_gives_no_brokers(tmp_path: Path) -> None:
    assert load_registry(tmp_path) == ()


def test_hidden_files_are_ignored(tmp_path: Path) -> None:
    write(tmp_path, "fr/.gitkeep", "")
    write(tmp_path, ".DS_Store", "")

    assert load_registry(tmp_path) == ()


def test_yes_no_on_off_are_not_booleans(tmp_path: Path) -> None:
    """PyYAML follows YAML 1.1, where `NO` (Norway) would silently become False."""
    data = broker_data("courtier-norvegien", "eu", notes="on")
    text = yaml.safe_dump(data, sort_keys=False).replace("country: FR", "country: NO")
    assert "country: NO" in text
    write(tmp_path, "eu/courtier-norvegien.yml", text)

    (broker,) = load_registry(tmp_path)

    assert broker.country == "NO"
    assert broker.notes == "on"


def test_true_and_false_are_still_booleans(tmp_path: Path) -> None:
    text = yaml.safe_dump(broker_data(), sort_keys=False)
    assert "requires_id: false" in text
    write(
        tmp_path, "fr/courtier-fictif.yml", text.replace("requires_id: false", "requires_id: true")
    )

    (broker,) = load_registry(tmp_path)

    assert broker.requires_id is True


# --- Errors: unreadable or malformed files --------------------------------------


def test_missing_root_is_an_error(tmp_path: Path) -> None:
    missing = tmp_path / "absent"

    (error,) = load_errors(missing)

    assert error.path == missing


def test_yaml_syntax_error_reports_file_and_line(tmp_path: Path) -> None:
    path = write(tmp_path, "fr/courtier-fictif.yml", "slug: courtier-fictif\nname: [non fermé\n")

    (error,) = load_errors(tmp_path)

    assert error.path == path
    assert "ligne" in error.message


def test_duplicate_yaml_keys_are_rejected(tmp_path: Path) -> None:
    text = yaml.safe_dump(broker_data(), sort_keys=False) + "slug: autre-slug\n"
    path = write(tmp_path, "fr/courtier-fictif.yml", text)

    (error,) = load_errors(tmp_path)

    assert error.path == path
    assert "slug" in error.message


@pytest.mark.parametrize("content", ["", "# commentaire seul\n", "- slug: x\n", "texte\n"])
def test_file_must_contain_a_mapping(tmp_path: Path, content: str) -> None:
    path = write(tmp_path, "fr/courtier-fictif.yml", content)

    (error,) = load_errors(tmp_path)

    assert error.path == path


def test_invalid_utf8_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "fr" / "courtier-fictif.yml"
    path.parent.mkdir()
    path.write_bytes(b"name: \xff\xfe\n")

    (error,) = load_errors(tmp_path)

    assert error.path == path


# --- Errors: schema validation ----------------------------------------------------


def test_validation_error_reports_file_and_field(tmp_path: Path) -> None:
    data = broker_data(channels=[{"type": "email", "address": "pas-une-adresse"}])
    path = write(tmp_path, "fr/courtier-fictif.yml", data)

    (error,) = load_errors(tmp_path)

    assert error.path == path
    assert error.field == "channels[0].address"
    assert str(error).startswith(f"{path} › channels[0].address : ")


def test_nested_step_errors_have_readable_fields(tmp_path: Path) -> None:
    channels = [
        {"type": "email", "address": "dpo@example.org"},
        {"type": "web_form", "url": "https://example.org/rgpd", "steps": [{"type": "inconnu"}]},
    ]
    write(tmp_path, "fr/courtier-fictif.yml", broker_data(channels=channels))

    (error,) = load_errors(tmp_path)

    assert error.field == "channels[1].steps[0].type"


def test_step_parameter_errors_have_readable_fields(tmp_path: Path) -> None:
    steps = [
        {"id": "open", "type": "navigate"},
        {
            "id": "fill",
            "type": "form_fill",
            "fields": {"email": {"selector": "#e", "value": "{x}"}},
        },
    ]
    channels = [{"type": "web_form", "url": "https://example.org/rgpd", "steps": steps}]
    write(tmp_path, "fr/courtier-fictif.yml", broker_data(channels=channels))

    (error,) = load_errors(tmp_path)

    assert error.field == "channels[0].steps[1].fields.email.value"
    assert "variable inconnue {x}" in error.message


def test_validation_errors_do_not_echo_values(tmp_path: Path) -> None:
    write(tmp_path, "fr/courtier-fictif.yml", broker_data(name="  ", country="valeur-secrete"))

    errors = load_errors(tmp_path)

    assert {error.field for error in errors} == {"name", "country"}
    assert all("valeur-secrete" not in str(error) for error in errors)


def test_all_errors_are_collected(tmp_path: Path) -> None:
    write(tmp_path, "fr/courtier-a.yml", broker_data("courtier-a", country="fr"))
    write(tmp_path, "fr/courtier-b.yml", "slug: [\n")
    write(tmp_path, "us/courtier-c.yml", broker_data("courtier-c", "us", source_ref=""))
    write(tmp_path, "us/courtier-d.yml", broker_data("courtier-d", "us"))

    errors = load_errors(tmp_path)

    assert sorted(error.path.name for error in errors) == [
        "courtier-a.yml",
        "courtier-b.yml",
        "courtier-c.yml",
    ]


# --- Errors: file layout and cross-file rules --------------------------------------


def test_file_name_must_match_slug(tmp_path: Path) -> None:
    path = write(tmp_path, "fr/mauvais-nom.yml", broker_data("courtier-fictif"))

    (error,) = load_errors(tmp_path)

    assert error.path == path
    assert error.field == "slug"


def test_directory_must_match_region(tmp_path: Path) -> None:
    path = write(tmp_path, "us/courtier-fictif.yml", broker_data(region="fr"))

    (error,) = load_errors(tmp_path)

    assert error.path == path
    assert error.field == "region"


def test_slugs_must_be_unique_across_regions(tmp_path: Path) -> None:
    first = write(tmp_path, "fr/courtier-fictif.yml", broker_data(region="fr"))
    second = write(tmp_path, "eu/courtier-fictif.yml", broker_data(region="eu"))

    (error,) = load_errors(tmp_path)

    assert error.path == second
    assert error.field == "slug"
    assert str(first) in error.message


def test_yaml_extension_is_rejected(tmp_path: Path) -> None:
    path = write(tmp_path, "fr/courtier-fictif.yaml", broker_data())

    (error,) = load_errors(tmp_path)

    assert error.path == path
    assert ".yml" in error.message


def test_unknown_region_directory_is_rejected(tmp_path: Path) -> None:
    write(tmp_path, "de/courtier-fictif.yml", broker_data())

    (error,) = load_errors(tmp_path)

    assert error.path == tmp_path / "de"


def test_subdirectory_inside_region_is_rejected(tmp_path: Path) -> None:
    write(tmp_path, "fr/archives/courtier-fictif.yml", broker_data())

    (error,) = load_errors(tmp_path)

    assert error.path == tmp_path / "fr" / "archives"


@pytest.mark.parametrize("name", ["courtier-fictif.yml", "courtier-fictif.yaml"])
def test_definition_outside_region_directory_is_rejected(tmp_path: Path, name: str) -> None:
    path = write(tmp_path, name, broker_data())

    (error,) = load_errors(tmp_path)

    assert error.path == path


def test_registry_error_message_lists_every_problem(tmp_path: Path) -> None:
    write(tmp_path, "fr/courtier-a.yml", broker_data("courtier-a", country="fr"))
    write(tmp_path, "fr/courtier-b.yml", broker_data("courtier-b", language="FR"))

    with pytest.raises(RegistryError) as exc_info:
        load_registry(tmp_path)

    message = str(exc_info.value)
    assert "courtier-a.yml › country" in message
    assert "courtier-b.yml › language" in message


@pytest.mark.skipif(not yaml.__with_libyaml__, reason="PyYAML built without libyaml")
def test_registry_parses_with_libyaml() -> None:
    """Several hundred files: the pure-Python parser is about eight times slower."""
    from gomme.brokers import registry

    assert issubclass(registry._StrictYamlLoader, yaml.CSafeLoader)
