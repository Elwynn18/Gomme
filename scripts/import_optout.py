"""Import OptOut's broker definitions into the Gomme schema.

OptOut (https://github.com/Blake104/OptOut, AGPL-3.0-only) ships a few US people-search
brokers whose web forms were verified end to end. Its steps are converted to Gomme's,
its placeholders renamed to Gomme's closed list, and its texts given a French version
from scripts/optout_fr.yml.

Usage, from the repository root, with local clean clones of the upstream projects:

    uv run python scripts/import_optout.py --repo ../OptOut [--eraser-repo ../eraser]

With --eraser-repo, a broker OptOut gives no email address for gets Eraser's, and the
matching Eraser entry is referenced in the notes. The script never accesses the network.
"""

import argparse
import re
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml
from _import_common import (
    EraserEntry,
    ImportReport,
    UpstreamError,
    git_head,
    normalize_domain,
    read_eraser,
    write_definitions,
)

from gomme.brokers.fields import ProfileField
from gomme.brokers.registry import default_brokers_dir
from gomme.brokers.schema import Broker, Source, WebFormChannel

OPTOUT_REPO_URL = "https://github.com/Blake104/OptOut"
OPTOUT_BROKERS_PATH = "src/optout/data/brokers"
TRANSLATIONS_PATH = Path(__file__).with_name("optout_fr.yml")

# OptOut placeholder -> Gomme placeholder. OptOut also allows any attribute of its
# profile object; anything not listed here is refused.
PLACEHOLDER_NAMES = {
    "name_first": "first_name",
    "name_last": "last_name",
    "profile.legal_name": "full_name",
    "name_slug": "name_slug",
    "name_title_slug": "name_title_slug",
    "profile.emails.current[0]": "email",
    "profile.current_address.street": "street",
    "profile.current_address.city": "city",
    "profile.current_address.zip": "postal_code",
    "state": "state",
    "state_name": "state_name",
    "state_full": "state_slug",  # OptOut's state_full is hyphenated: New-York
    "age": "age",
    "birth_year": "birth_year",
    "phone_current": "phone",
    "state.found_listing_url": "listing_url",
}

# OptOut field names in required_fields / optional_fields.
PROFILE_FIELDS = {
    "full_name": ProfileField.FULL_NAME,
    "current_address": ProfileField.POSTAL_ADDRESS,
    "email_current": ProfileField.EMAIL,
    "dob": ProfileField.BIRTH_DATE,
    "phone_current": ProfileField.PHONE,
    "listing_url": None,  # found during the run, not profile data
}

CATEGORIES = {"people_search": "people-search"}

LEGAL_BASES = {
    "CCPA": ["ccpa_1798_105"],
    "CPRA": ["ccpa_1798_105"],  # CPRA amended §1798.105, it is not a separate right
    "GDPR": ["gdpr_art17", "gdpr_art21"],
}

_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")

_TOP_LEVEL_KEYS = frozenset(
    {
        "slug",
        "name",
        "domain",
        "category",
        "parent_company",
        "method",
        "opt_out_url",
        "opt_out_email",
        "opt_out_postal_address",
        "required_fields",
        "optional_fields",
        "requires_listing_url",
        "requires_phone_verification",
        "requires_email_verification",
        "requires_id_upload",
        "requires_notarization",
        "legal_basis",
        "statutory_response_days",
        "re_add_window_days",
        "last_verified_at",
        "notes",
        "scan",
        "steps",
    }
)


def convert_template(text: str) -> str:
    """Rename OptOut placeholders; refuse any other one."""

    def rename(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in PLACEHOLDER_NAMES:
            raise UpstreamError(f"variable OptOut non prise en charge : {{{name}}}")
        return "{" + PLACEHOLDER_NAMES[name] + "}"

    return _PLACEHOLDER.sub(rename, text)


def _text(english: str | None, french: str | None) -> dict[str, str] | None:
    if english is None:
        return None
    text = {"en": convert_template(english)}
    return {"fr": french, **text} if french else text


def convert_step(
    raw: Mapping[str, Any], french: Mapping[str, str], missing: Callable[[str], None]
) -> dict[str, Any]:
    """One OptOut step as a Gomme step. Unknown or unsupported parameters are refused."""
    step = dict(raw)
    step_type = step.pop("type")
    step_id = step.pop("id")
    out: dict[str, Any] = {"id": step_id, "type": step_type}

    def take(key: str, convert: Callable[[Any], Any] = convert_template) -> None:
        if key in step:
            value = step.pop(key)
            if value is not None:
                out[key] = convert(value)

    def take_text(key: str) -> None:
        if key in step:
            value = _text(step.pop(key), french.get(key))
            if value is not None:
                out[key] = value
                if "fr" not in value:
                    missing(f"{step_id}.{key}")

    take_text("label")
    if step.pop("retry", None) is not None or step.pop("on_failure", "abort") != "abort":
        raise UpstreamError(f"étape {step_id} : retry / on_failure ne sont pas repris")

    match step_type:
        case "navigate":
            take("url")
        case "search":
            if step.pop("pick_strategy", "prompt_user") != "prompt_user":
                raise UpstreamError(f"étape {step_id} : seul pick_strategy: prompt_user existe")
            out["url"] = convert_template(step.pop("search_url_template"))
            take("result_selector")
        case "form_fill":
            take("frame")
            out["fields"] = {
                name: _convert_field(name, field) for name, field in step.pop("fields").items()
            }
        case "click":
            take("selector")
            take("frame")
        case "wait_for":
            take("selector")
            take("js_condition", convert=str)
            take("timeout_ms", convert=int)
            take_text("message")
            take("frame")
        case "prompt_user":
            take_text("message")
        case "prompt_user_if_present":
            take("selector")
            take_text("message")
            take("frame")
        case "assert":
            take("selector")
            take("text_contains")
            take("url_matches", convert=str)
            take("js_condition", convert=str)
        case "capture":
            if step.pop("method", "screenshot") != "screenshot":
                raise UpstreamError(f"étape {step_id} : seule la capture d'écran existe")
            step.pop("save_as", None)  # Gomme names its proofs itself
        case _:
            raise UpstreamError(f"étape {step_id} : type « {step_type} » non pris en charge")

    if step.pop("optional", False):
        out["optional"] = True
    if step:
        raise UpstreamError(f"étape {step_id} : paramètres inconnus {sorted(step)}")
    return out


def _convert_field(name: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    field = dict(raw)
    out: dict[str, Any] = {
        "selector": field.pop("selector"),
        "value": convert_template(field.pop("value")),
    }
    use_type, use_js = field.pop("use_type", False), field.pop("use_js", False)
    if use_type and use_js:
        raise UpstreamError(f"champ {name} : use_type et use_js à la fois")
    if use_type:
        out["input"] = "type"
    elif use_js:
        out["input"] = "js"
    if field.pop("force", False):
        out["force"] = True
    field.pop("is_select", None)  # the engine recognises <select> elements itself
    if field:
        raise UpstreamError(f"champ {name} : paramètres inconnus {sorted(field)}")
    return out


def convert_broker(
    raw: Mapping[str, Any],
    commit: str,
    french: Mapping[str, Mapping[str, str]],
    eraser: EraserEntry | None,
    missing: Callable[[str], None],
) -> dict[str, Any]:
    """One OptOut definition as a Gomme definition (a plain dict, in canonical order)."""
    unknown = set(raw) - _TOP_LEVEL_KEYS
    if unknown:
        raise UpstreamError(f"clés inconnues : {sorted(unknown)}")
    slug = raw["slug"]
    if raw.get("requires_notarization"):
        raise UpstreamError("une demande notariée ne peut pas être préparée par Gomme")

    known_steps = {step["id"] for step in raw.get("steps", [])}
    stale = set(french) - known_steps
    if stale:
        raise UpstreamError(f"traductions sans étape correspondante : {sorted(stale)}")

    channels: list[dict[str, Any]] = []
    if raw["method"] == "web_form":
        steps = [
            convert_step(step, french.get(step["id"], {}), lambda key: missing(f"{slug}.{key}"))
            for step in raw.get("steps", [])
        ]
        channels.append({"type": "web_form", "url": raw["opt_out_url"], "steps": steps})
    elif raw["method"] != "email":
        raise UpstreamError(f"méthode « {raw['method']} » non prise en charge")

    email = raw.get("opt_out_email") or (eraser.email if eraser else "")
    if email:
        channels.append({"type": "email", "address": email})
    if raw.get("opt_out_postal_address"):
        channels.append({"type": "postal", "address": raw["opt_out_postal_address"]})

    legal_basis: list[str] = []
    for basis in raw["legal_basis"]:
        if basis not in LEGAL_BASES:
            raise UpstreamError(f"base légale inconnue : {basis}")
        legal_basis += [item for item in LEGAL_BASES[basis] if item not in legal_basis]

    data: dict[str, Any] = {
        "slug": slug,
        "name": raw["name"],
        "domain": normalize_domain(raw["domain"]),
        "region": "us",
        "category": CATEGORIES[raw["category"]],
        "channels": channels,
        "legal_basis": legal_basis,
        "required_fields": _required_fields(raw, channels),
        "requires_id": bool(raw.get("requires_id_upload", False)),
        "language": "en",
        "source": "optout",
        "source_ref": f"{OPTOUT_REPO_URL}/blob/{commit}/{OPTOUT_BROKERS_PATH}/{slug}.yml",
        "last_verified": raw.get("last_verified_at"),
        "notes": _notes(raw, eraser, email_from_eraser=bool(email) and not raw["opt_out_email"]),
    }
    if data["last_verified"] is None:
        del data["last_verified"]
    return data


def _required_fields(raw: Mapping[str, Any], channels: list[dict[str, Any]]) -> list[str]:
    """Fields OptOut declares as required, plus every field its steps actually send.

    OptOut's lists are not always complete: some forms fill in fields declared optional.
    """
    fields: set[ProfileField] = set()
    for name in raw.get("required_fields", []):
        if name not in PROFILE_FIELDS:
            raise UpstreamError(f"champ requis inconnu : {name}")
        field = PROFILE_FIELDS[name]
        if field is not None:
            fields.add(field)
    for channel in channels:
        if channel["type"] == "web_form":
            parsed = WebFormChannel.model_validate(channel)
            fields |= parsed.profile_fields().keys()
        elif channel["type"] == "email":
            fields |= {ProfileField.FULL_NAME, ProfileField.EMAIL}
    return [field.value for field in ProfileField if field in fields]


def _notes(raw: Mapping[str, Any], eraser: EraserEntry | None, *, email_from_eraser: bool) -> str:
    lines = []
    if raw.get("requires_email_verification"):
        lines.append("Confirmation requise : le broker envoie un lien par email.")
    if raw.get("requires_phone_verification"):
        lines.append("Vérification par téléphone : le broker appelle le numéro de la fiche.")
    if raw.get("re_add_window_days"):
        lines.append(
            f"Selon OptOut, les fiches réapparaissent souvent sous "
            f"{raw['re_add_window_days']} jours."
        )
    if eraser is not None:
        origin = "adresse email reprise d'Eraser" if email_from_eraser else "aussi listé par Eraser"
        lines.append(f"Eraser : {origin}, {eraser.permalink}")
        if eraser.notes:
            lines.append(f"Notes d'Eraser : {eraser.notes.strip()}")
    if raw.get("notes"):
        lines.append(f"Notes d'OptOut :\n{raw['notes'].strip()}")
    return "\n\n".join(lines)


def import_optout(
    repo: Path,
    brokers_dir: Path,
    eraser_repo: Path | None = None,
    translations_path: Path = TRANSLATIONS_PATH,
) -> ImportReport:
    commit = git_head(repo)
    eraser_by_domain: dict[str, EraserEntry] = {}
    if eraser_repo is not None:
        eraser_by_domain = {entry.domain: entry for entry in read_eraser(eraser_repo)}
    translations: dict[str, dict[str, dict[str, str]]] = (
        yaml.safe_load(translations_path.read_text(encoding="utf-8")) or {}
    )

    report = ImportReport()
    missing: list[str] = []
    definitions = []
    files = sorted((repo / OPTOUT_BROKERS_PATH).glob("*.yml"))
    if not files:
        raise UpstreamError(f"aucune fiche dans {repo / OPTOUT_BROKERS_PATH}")
    for path in files:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        try:
            data = convert_broker(
                raw,
                commit,
                translations.get(raw["slug"], {}),
                eraser_by_domain.get(normalize_domain(raw["domain"])),
                missing.append,
            )
            Broker.model_validate(data)
        except (UpstreamError, KeyError, ValueError) as exc:
            raise UpstreamError(f"{path.name} : {exc}") from exc
        definitions.append((raw["slug"], data))

    stale = set(translations) - {slug for slug, _ in definitions}
    if stale:
        raise UpstreamError(f"traductions pour des fiches absentes : {sorted(stale)}")

    header = [
        f"Généré par scripts/import_optout.py depuis {OPTOUT_REPO_URL}",
        f"(commit {commit}, licence AGPL-3.0-only).",
        "Ne pas modifier à la main : corriger le script ou ses traductions, puis le relancer.",
    ]
    write_definitions(brokers_dir, Source.OPTOUT, definitions, header, report)
    report.warnings += [f"texte sans traduction française : {key}" for key in missing]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", type=Path, required=True, help="clone local d'OptOut")
    parser.add_argument("--eraser-repo", type=Path, help="clone local d'Eraser (facultatif)")
    parser.add_argument("--brokers-dir", type=Path, default=default_brokers_dir())
    args = parser.parse_args(argv)
    try:
        report = import_optout(args.repo, args.brokers_dir, args.eraser_repo)
    except UpstreamError as exc:
        print(f"Import impossible : {exc}", file=sys.stderr)
        return 1
    report.print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
