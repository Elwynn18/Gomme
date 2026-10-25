"""Load and validate broker definitions from a directory of YAML files.

Layout: ``<root>/<region>/<slug>.yml``, one broker per file. Every problem found is
collected and reported at once, each with its file and, when known, its field.
"""

import re
from collections.abc import Hashable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args

import pydantic
import yaml

from gomme.brokers.schema import (
    Broker,
    EmailChannel,
    PostalChannel,
    Region,
    StepType,
    WebFormChannel,
)

BROKER_SUFFIX = ".yml"

_BOOL_TAG = "tag:yaml.org,2002:bool"
_MERGE_TAG = "tag:yaml.org,2002:merge"
# Pydantic inserts the discriminator value in error locations of tagged unions
# (channels.0.email.address); we drop it to show the path as written in the YAML.
_UNION_TAGS = {
    "channels": frozenset(
        get_args(model.model_fields["type"].annotation)[0]
        for model in (EmailChannel, WebFormChannel, PostalChannel)
    ),
    "steps": frozenset(step.value for step in StepType),
}
# For these errors pydantic points at the item; the culprit is its `type` key.
_TAG_ERRORS = frozenset({"union_tag_invalid", "union_tag_not_found"})


@dataclass(frozen=True, slots=True)
class BrokerFileError:
    """One problem in the registry, tied to a file (or directory) and optionally a field."""

    path: Path
    message: str
    field: str | None = None

    def __str__(self) -> str:
        where = f"{self.path} › {self.field}" if self.field else str(self.path)
        return f"{where} : {self.message}"


class RegistryError(Exception):
    """The registry could not be loaded; `errors` lists every problem found."""

    def __init__(self, errors: Sequence[BrokerFileError]) -> None:
        self.errors = tuple(errors)
        super().__init__("\n".join(str(error) for error in self.errors))


# libyaml parses about eight times faster, which matters with several hundred files.
# Only the parser is in C: the Python constructor and resolver below still apply.
try:
    from yaml import CSafeLoader as _SafeLoader
except ImportError:  # pragma: no cover - PyYAML built without libyaml
    from yaml import SafeLoader as _SafeLoader  # type: ignore[assignment]


class _StrictYamlLoader(_SafeLoader):
    """SafeLoader with YAML 1.2 booleans and duplicate keys rejected.

    PyYAML follows YAML 1.1, where yes/no/on/off are booleans: ``country: NO``
    (Norway) would silently become False. Duplicate keys would silently keep the last.
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        seen: set[Hashable] = set()
        for key_node, _ in node.value:
            if key_node.tag == _MERGE_TAG:
                continue
            key = self.construct_object(key_node, deep=deep)
            if isinstance(key, Hashable) and key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"clé en double : {key!r}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


_StrictYamlLoader.yaml_implicit_resolvers = {
    first_char: [(tag, regexp) for tag, regexp in resolvers if tag != _BOOL_TAG]
    for first_char, resolvers in _SafeLoader.yaml_implicit_resolvers.items()
}
_StrictYamlLoader.add_implicit_resolver(
    _BOOL_TAG, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


def load_strict_yaml(text: str) -> Any:
    """Parse YAML safely, with YAML 1.2 booleans and no duplicate keys."""
    return yaml.load(text, Loader=_StrictYamlLoader)  # noqa: S506 -- SafeLoader subclass


def describe_yaml_error(exc: yaml.YAMLError) -> str:
    if isinstance(exc, yaml.MarkedYAMLError):
        return _describe_yaml_error(exc)
    return "YAML invalide"


def default_brokers_dir() -> Path:
    """Broker definitions shipped with the package."""
    return Path(__file__).resolve().parent.parent / "data" / "brokers"


def load_registry(root: Path) -> tuple[Broker, ...]:
    """Load every broker under `root`, ordered by region then slug.

    Raises RegistryError listing all problems if any file is invalid.
    """
    if not root.is_dir():
        raise RegistryError([BrokerFileError(root, "dossier de fiches introuvable")])

    errors = _check_root_layout(root)
    loaded: list[tuple[Path, Broker]] = []
    for region in Region:
        region_dir = root / region.value
        if not region_dir.is_dir():
            continue
        for path in sorted(_visible_entries(region_dir)):
            if path.is_dir():
                errors.append(
                    BrokerFileError(
                        path, f"sous-dossier non pris en charge, placer les fiches dans {region}/"
                    )
                )
            elif path.suffix == ".yaml":
                errors.append(
                    BrokerFileError(path, f"extension non reconnue, renommer en {BROKER_SUFFIX}")
                )
            elif path.suffix == BROKER_SUFFIX:
                try:
                    broker = load_broker_file(path)
                except RegistryError as exc:
                    errors.extend(exc.errors)
                    continue
                errors.extend(_check_placement(path, region, broker))
                loaded.append((path, broker))

    errors.extend(_check_unique_slugs(loaded))
    if errors:
        raise RegistryError(errors)
    return tuple(broker for _, broker in loaded)


def load_broker_file(path: Path) -> Broker:
    """Parse and validate a single broker definition."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise RegistryError(
            [BrokerFileError(path, "le fichier n'est pas encodé en UTF-8")]
        ) from None
    except OSError as exc:
        raise RegistryError(
            [BrokerFileError(path, f"lecture impossible ({exc.strerror})")]
        ) from None

    try:
        raw = load_strict_yaml(text)
    except yaml.MarkedYAMLError as exc:
        raise RegistryError([BrokerFileError(path, _describe_yaml_error(exc))]) from None
    except yaml.YAMLError:
        raise RegistryError([BrokerFileError(path, "YAML invalide")]) from None

    if raw is None:
        raise RegistryError([BrokerFileError(path, "fichier vide")])
    if not isinstance(raw, dict):
        raise RegistryError(
            [BrokerFileError(path, "le fichier doit contenir des paires « clé: valeur »")]
        )

    try:
        return Broker.model_validate(raw)
    except pydantic.ValidationError as exc:
        raise RegistryError(
            [
                BrokerFileError(
                    path,
                    error["msg"],
                    field=_format_location(
                        (*error["loc"], "type") if error["type"] in _TAG_ERRORS else error["loc"]
                    )
                    or None,
                )
                for error in exc.errors(include_url=False, include_input=False)
            ]
        ) from None


def _visible_entries(directory: Path) -> Iterator[Path]:
    return (entry for entry in directory.iterdir() if not entry.name.startswith("."))


def _check_root_layout(root: Path) -> list[BrokerFileError]:
    region_names = [region.value for region in Region]
    errors: list[BrokerFileError] = []
    for entry in sorted(_visible_entries(root)):
        if entry.is_dir() and entry.name not in region_names:
            expected = ", ".join(f"{name}/" for name in region_names)
            errors.append(
                BrokerFileError(entry, f"dossier de région inconnu (attendus : {expected})")
            )
        elif entry.is_file() and entry.suffix in (BROKER_SUFFIX, ".yaml"):
            errors.append(BrokerFileError(entry, "fiche hors d'un dossier de région"))
    return errors


def _check_placement(path: Path, region: Region, broker: Broker) -> list[BrokerFileError]:
    errors: list[BrokerFileError] = []
    if path.stem != broker.slug:
        errors.append(
            BrokerFileError(
                path, f"le nom du fichier doit être le slug : {broker.slug}{BROKER_SUFFIX}", "slug"
            )
        )
    if broker.region is not region:
        errors.append(
            BrokerFileError(
                path, f"région « {broker.region} » rangée dans le dossier {region}/", "region"
            )
        )
    return errors


def _check_unique_slugs(loaded: Iterable[tuple[Path, Broker]]) -> list[BrokerFileError]:
    first_seen: dict[str, Path] = {}
    errors: list[BrokerFileError] = []
    for path, broker in loaded:
        if broker.slug in first_seen:
            errors.append(
                BrokerFileError(path, f"slug déjà utilisé par {first_seen[broker.slug]}", "slug")
            )
        else:
            first_seen[broker.slug] = path
    return errors


def _describe_yaml_error(exc: yaml.MarkedYAMLError) -> str:
    problem = exc.problem or "erreur de syntaxe"
    mark = exc.problem_mark
    if mark is None:
        return f"YAML invalide : {problem}"
    return f"YAML invalide (ligne {mark.line + 1}, colonne {mark.column + 1}) : {problem}"


def _format_location(location: tuple[int | str, ...]) -> str:
    """Render a pydantic error location as written in YAML: ``channels[0].address``."""
    rendered = ""
    for index, item in enumerate(location):
        if isinstance(item, int):
            rendered += f"[{item}]"
        elif index >= 2 and item in _UNION_TAGS.get(str(location[index - 2]), ()):
            continue
        else:
            rendered += f".{item}" if rendered else item
    return rendered
