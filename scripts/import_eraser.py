"""Import US and global brokers from Eraser's broker list into the Gomme schema.

Eraser (https://github.com/drumandbytes/eraser, MIT; the maintained fork of
digisamroc/eraser) lists several hundred brokers, mostly US marketing companies, each
with a privacy email address and/or an opt-out page. Only `us` and `global` entries are
imported: European definitions are written and verified by hand.

Usage, from the repository root, with a local clean clone of Eraser:

    uv run python scripts/import_eraser.py --repo ../eraser

Run scripts/import_optout.py first: brokers OptOut covers are skipped here. The script
never accesses the network.
"""

import argparse
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pydantic
from _import_common import (
    ERASER_REPO_URL,
    EraserEntry,
    ImportReport,
    UpstreamError,
    git_head,
    read_eraser,
    write_definitions,
)

from gomme.brokers.registry import default_brokers_dir
from gomme.brokers.schema import Broker, Source

IMPORTED_REGIONS = frozenset({"us", "global"})

CATEGORIES = {
    "marketing": "marketing",
    "people-search": "people-search",
    "background-check": "background-check",
    "financial-b2b": "autre",
    "requires-id": "autre",  # Eraser's category for brokers demanding an ID document
}

# Brokers that only hold advertising or device identifiers: a request made with a name
# and an email address cannot match anything.
DEVICE_ID_ONLY = "device-id-only"

# National credit bureaus Eraser files under `global`, outside Gomme's scope (France and
# Europe first, then the United States).
OUT_OF_SCOPE = {
    "nosis": "bureau de crédit argentin",
    "circulo-de-credito": "bureau de crédit mexicain",
    "buro-de-credito-mx": "bureau de crédit mexicain",
    "datacredito-experian-co": "bureau de crédit colombien",
    "cial-dun-bradstreet": "réseau de crédit d'Amérique latine",
    "centrix-nz": "bureau de crédit néo-zélandais",
    "metropol-crb": "bureau de crédit kényan",
}

LEGAL_BASES = {
    "us": ["ccpa_1798_105"],
    # Worldwide brokers: GDPR for a user in Europe (erasure and objection, always
    # together), CCPA for a user in California.
    "global": ["gdpr_art17", "gdpr_art21", "ccpa_1798_105"],
}

# People-search and background-check sites index people by address: a name alone is
# ambiguous. Marketing brokers match on the email address.
NEEDS_ADDRESS = frozenset({"people-search", "background-check"})


def convert_entry(entry: EraserEntry) -> dict[str, Any]:
    """One Eraser entry as a Gomme definition (a plain dict, in canonical order)."""
    channels: list[dict[str, Any]] = []
    if entry.email:
        channels.append({"type": "email", "address": entry.email})
    if entry.opt_out_url:
        channels.append({"type": "web_form", "url": entry.opt_out_url})

    category = CATEGORIES[entry.category]
    required_fields = ["full_name", "email"]
    if category in NEEDS_ADDRESS:
        required_fields.append("postal_address")

    notes = []
    if entry.category == "requires-id":
        notes.append(
            "Selon Eraser, ce broker exige une pièce d'identité. Ne l'envoyez jamais sans "
            "l'avoir caviardée."
        )
    if entry.notes.strip():
        notes.append(f"Notes d'Eraser : {entry.notes.strip()}")

    return {
        "slug": entry.id,
        "name": entry.name.strip(),
        "domain": entry.domain,
        "region": entry.region,
        "category": category,
        "channels": channels,
        "legal_basis": LEGAL_BASES[entry.region],
        "required_fields": required_fields,
        "requires_id": entry.category == "requires-id",
        "language": "en",
        "source": "eraser",
        "source_ref": entry.permalink,
        "notes": "\n\n".join(notes),
    }


def select_entries(
    entries: tuple[EraserEntry, ...], report: ImportReport
) -> Iterator[tuple[str, dict[str, Any]]]:
    """The entries to import, as (Eraser id, definition); the others go to the report."""
    for entry in entries:
        if entry.region not in IMPORTED_REGIONS:
            continue
        if entry.category == DEVICE_ID_ONLY:
            report.skipped.append((entry.id, "n'accepte qu'un identifiant publicitaire"))
        elif entry.id in OUT_OF_SCOPE:
            report.skipped.append((entry.id, f"hors périmètre : {OUT_OF_SCOPE[entry.id]}"))
        elif not entry.email and not entry.opt_out_url:
            report.skipped.append((entry.id, "aucun canal de contact"))
        elif entry.category not in CATEGORIES:
            raise UpstreamError(f"{entry.id} : catégorie Eraser inconnue « {entry.category} »")
        else:
            data = convert_entry(entry)
            try:
                Broker.model_validate(data)
            except pydantic.ValidationError as exc:
                problems = "; ".join(
                    f"{'.'.join(map(str, error['loc']))} : {error['msg']}"
                    for error in exc.errors(include_url=False, include_input=False)
                )
                report.skipped.append((entry.id, f"invalide ({problems})"))
                continue
            yield entry.id, data

    known = {entry.id for entry in entries}
    for stale in sorted(set(OUT_OF_SCOPE) - known):
        report.warnings.append(f"{stale} n'existe plus chez Eraser : le retirer de OUT_OF_SCOPE")


def import_eraser(repo: Path, brokers_dir: Path) -> ImportReport:
    commit = git_head(repo)
    entries = read_eraser(repo)
    report = ImportReport()
    header = [
        f"Généré par scripts/import_eraser.py depuis {ERASER_REPO_URL}",
        f"(commit {commit}, licence MIT, voir LICENSES/eraser-MIT.txt).",
        "Ne pas modifier à la main : corriger le script, puis le relancer.",
    ]
    definitions = list(select_entries(entries, report))
    write_definitions(brokers_dir, Source.ERASER, definitions, header, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", type=Path, required=True, help="clone local d'Eraser")
    parser.add_argument("--brokers-dir", type=Path, default=default_brokers_dir())
    args = parser.parse_args(argv)
    try:
        report = import_eraser(args.repo, args.brokers_dir)
    except UpstreamError as exc:
        print(f"Import impossible : {exc}", file=sys.stderr)
        return 1
    report.print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
