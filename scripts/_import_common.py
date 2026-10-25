"""Helpers shared by the scripts that import upstream broker lists.

Each importer owns the definitions whose `source` is its own: it rewrites them, and
deletes those upstream dropped. It never touches a definition from another source. On
the same slug or domain, a definition from a stronger source wins: manual entries
first, then OptOut (web forms verified end to end), then Eraser.
"""

import shutil
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pydantic
import yaml

from gomme.brokers.registry import BROKER_SUFFIX, load_registry
from gomme.brokers.schema import Broker, Source

SOURCE_RANK = {Source.MANUAL: 0, Source.OPTOUT: 1, Source.ERASER: 2}
"""Lower wins when two sources describe the same broker."""


class UpstreamError(Exception):
    """The upstream data cannot be imported as is."""


@dataclass
class ImportReport:
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (upstream id, reason)
    warnings: list[str] = field(default_factory=list)

    def print(self) -> None:
        print(
            f"{len(self.written)} écrite(s), {len(self.unchanged)} inchangée(s), "
            f"{len(self.removed)} supprimée(s), {len(self.skipped)} écartée(s)"
        )
        for slug in self.removed:
            print(f"  supprimée : {slug}")
        for upstream_id, reason in self.skipped:
            print(f"  écartée : {upstream_id} ({reason})")
        for warning in self.warnings:
            print(f"  attention : {warning}")


def git_head(repo: Path) -> str:
    """Commit of a local upstream checkout, which must be clean to be traceable."""
    git = shutil.which("git")
    if git is None:
        raise UpstreamError("git est introuvable")

    def run(*args: str) -> str:
        # Fixed arguments, no shell: the only input is the checkout path.
        result = subprocess.run(  # noqa: S603
            [git, "-C", str(repo), *args], capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            raise UpstreamError(f"{repo} n'est pas un dépôt git lisible : {result.stderr.strip()}")
        return result.stdout.strip()

    if run("status", "--porcelain"):
        raise UpstreamError(f"{repo} a des modifications locales : importer un commit propre")
    return run("rev-parse", "HEAD")


def normalize_domain(url_or_host: str) -> str:
    """``https://WWW.Example.org/x`` and ``www.example.org`` both give ``example.org``."""
    host = urlsplit(url_or_host).hostname if "//" in url_or_host else url_or_host
    host = (host or "").lower().rstrip(".")
    return host.removeprefix("www.")


class _Dumper(yaml.SafeDumper):
    """Readable output: indented lists, block style for multi-line text, no anchors."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        super().increase_indent(flow, indentless=False)

    def ignore_aliases(self, data: Any) -> bool:
        return True


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _represent_str)


def render_definition(data: Mapping[str, Any], header: Sequence[str]) -> str:
    """YAML text of a broker definition, after checking it against the schema."""
    Broker.model_validate(data)
    comments = "".join(f"# {line}\n" for line in header)
    # Optional keys left empty are omitted, to keep definitions short.
    shown = {key: value for key, value in data.items() if value not in ("", None)}
    body = yaml.dump(shown, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100)
    return comments + body


def write_definitions(
    brokers_dir: Path,
    source: Source,
    definitions: Iterable[tuple[str, Mapping[str, Any]]],
    header: Sequence[str],
    report: ImportReport,
) -> None:
    """Write the definitions imported from `source`, given as (upstream id, data) pairs."""
    existing = load_registry(brokers_dir)
    owned = {broker.slug: broker for broker in existing if broker.source is source}
    stronger = [
        broker for broker in existing if SOURCE_RANK.get(broker.source, 99) < SOURCE_RANK[source]
    ]
    taken_slugs = {broker.slug: broker for broker in existing if broker.source is not source}
    taken_domains = {normalize_domain(broker.domain): broker for broker in stronger}

    produced: dict[str, str] = {}  # slug -> region
    for upstream_id, data in definitions:
        slug, domain = data["slug"], normalize_domain(data["domain"])
        if slug in produced:
            raise UpstreamError(f"slug produit deux fois : {slug}")
        if slug in taken_slugs:
            other = taken_slugs[slug]
            report.skipped.append((upstream_id, f"slug déjà pris par une fiche {other.source}"))
            continue
        if domain in taken_domains:
            other = taken_domains[domain]
            report.skipped.append(
                (upstream_id, f"domaine déjà couvert par {other.slug} ({other.source})")
            )
            continue
        produced[slug] = data["region"]
        path = brokers_dir / data["region"] / f"{slug}{BROKER_SUFFIX}"
        text = render_definition(data, header)
        if path.exists() and path.read_text(encoding="utf-8") == text:
            report.unchanged.append(slug)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        report.written.append(slug)

    for slug, broker in sorted(owned.items()):
        if produced.get(slug) != broker.region:  # dropped upstream, or moved to another region
            (brokers_dir / broker.region / f"{slug}{BROKER_SUFFIX}").unlink()
            if slug not in produced:
                report.removed.append(slug)


# --- Eraser's data/brokers.yaml ---------------------------------------------------

ERASER_REPO_URL = "https://github.com/drumandbytes/eraser"
ERASER_DATA_PATH = "data/brokers.yaml"


class EraserEntry(pydantic.BaseModel):
    """One entry of Eraser's list, as found upstream."""

    model_config = pydantic.ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    email: str = ""
    website: str = ""
    opt_out_url: str = ""
    region: str
    category: str = ""
    notes: str = ""
    line: int  # 1-based line of the entry in brokers.yaml
    commit: str

    @property
    def permalink(self) -> str:
        return f"{ERASER_REPO_URL}/blob/{self.commit}/{ERASER_DATA_PATH}#L{self.line}"

    @property
    def domain(self) -> str:
        if self.website:
            return normalize_domain(self.website)
        if self.email:
            return normalize_domain(self.email.rpartition("@")[2])
        return normalize_domain(self.opt_out_url)


def read_eraser(repo: Path) -> tuple[EraserEntry, ...]:
    """Entries of Eraser's broker list, each with its commit and line number."""
    commit = git_head(repo)
    path = repo / ERASER_DATA_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UpstreamError(f"{path} illisible ({exc.strerror})") from None
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    data = yaml.safe_load(text)
    if (
        not isinstance(root, yaml.MappingNode)
        or not isinstance(data, dict)
        or not isinstance(data.get("brokers"), list)
    ):
        raise UpstreamError(f"{path} : clé « brokers » attendue")
    (brokers_node,) = (value for key, value in root.value if key.value == "brokers")
    entries = []
    for node, raw in zip(brokers_node.value, data["brokers"], strict=True):
        line = node.start_mark.line + 1
        try:
            entries.append(EraserEntry.model_validate({**raw, "line": line, "commit": commit}))
        except pydantic.ValidationError as exc:
            raise UpstreamError(f"{path}:{line} : {exc}") from None
    return tuple(entries)
