"""Web form engine: walk a broker's declarative steps in a visible browser.

The engine only talks to a `Page` and a `Prompter`, so it runs the same against
Playwright (channels/browser.py) and against fakes in tests. It never solves a CAPTCHA:
`prompt_user` steps hand the browser over to the user, who acts and confirms.

Personal data only enters a step through placeholders, filled with the values of the
broker's `required_fields` (see gomme.profile.placeholder_values): a step cannot reach
anything else.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from gomme.brokers.fields import LISTING_URL, render
from gomme.brokers.schema import (
    AssertStep,
    CaptureStep,
    ClickStep,
    FormFillStep,
    InputMethod,
    LocalizedText,
    NavigateStep,
    PromptUserIfPresentStep,
    PromptUserStep,
    SearchStep,
    WaitForStep,
    WebFormChannel,
    WebFormStep,
)

DEFAULT_TIMEOUT_MS = 15_000


class Page(Protocol):
    """What the engine needs from a browser page."""

    def goto(self, url: str) -> None: ...
    def fill(
        self, selector: str, value: str, *, frame: str | None, method: InputMethod, force: bool
    ) -> None: ...
    def click(self, selector: str, *, frame: str | None) -> None: ...
    def wait_for_selector(self, selector: str, *, frame: str | None, timeout_ms: int) -> None: ...
    def wait_for_condition(self, js_condition: str, *, timeout_ms: int) -> None: ...
    def is_present(self, selector: str, *, frame: str | None) -> bool: ...
    def text_of(self, selector: str | None) -> str: ...
    def evaluate(self, js_condition: str) -> bool: ...
    def url(self) -> str: ...
    def screenshot(self, path: Path) -> None: ...


class Prompter(Protocol):
    """How the engine talks to the user while the browser is open."""

    def show(self, message: str) -> None: ...
    def pause(self, message: str) -> None: ...  # returns once the user has acted
    def ask(self, message: str) -> str: ...


class StepFailedError(Exception):
    def __init__(self, step_id: str, reason: str) -> None:
        self.step_id = step_id
        super().__init__(f"étape {step_id} : {reason}")


@dataclass
class RunResult:
    listing_url: str | None = None
    screenshots: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # failed optional steps


MESSAGES = {
    "search": LocalizedText(
        fr=(
            "Le navigateur affiche les résultats de recherche. Ouvrez VOTRE fiche (et "
            "seulement la vôtre), puis revenez ici."
        ),
        en="The browser shows the search results. Open YOUR listing (only yours), then come back.",
    ),
    "listing_url": LocalizedText(
        fr="Collez l'adresse (URL) de votre fiche :",
        en="Paste the address (URL) of your listing:",
    ),
}


def run_steps(
    channel: WebFormChannel,
    page: Page,
    prompter: Prompter,
    values: Mapping[str, str],
    *,
    language: str,
    proof_path: Path,
) -> RunResult:
    """Run every step in order. A failing step stops the run unless it is optional."""
    state = dict(values)
    result = RunResult()
    for step in channel.steps:
        label = step.label.get(language) if step.label else step.id
        prompter.show(f"→ {label}")
        try:
            _run_step(step, channel, page, prompter, state, language, result, proof_path)
        except StepFailedError as exc:
            if not step.optional:
                raise
            result.warnings.append(str(exc))
        except Exception as exc:  # browser errors (timeouts, detached elements...)
            failure = StepFailedError(step.id, _describe(exc))
            if not step.optional:
                raise failure from exc
            result.warnings.append(str(failure))
    result.listing_url = state.get(LISTING_URL)
    return result


def _describe(exc: Exception) -> str:
    """A short reason, without the values the browser may echo back."""
    first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}" + (f" ({first_line[:120]})" if "Timeout" in first_line else "")


def _run_step(
    step: WebFormStep,
    channel: WebFormChannel,
    page: Page,
    prompter: Prompter,
    state: dict[str, str],
    language: str,
    result: RunResult,
    proof_path: Path,
) -> None:
    def fill_in(template: str) -> str:
        return render(template, state)

    match step:
        case NavigateStep():
            page.goto(fill_in(step.url) if step.url else str(channel.url))
        case SearchStep():
            search_url = fill_in(step.url)
            page.goto(search_url)
            page.wait_for_selector(
                fill_in(step.result_selector), frame=None, timeout_ms=DEFAULT_TIMEOUT_MS
            )
            prompter.pause(MESSAGES["search"].get(language))
            listing = page.url()
            if not listing or listing == search_url:
                listing = prompter.ask(MESSAGES["listing_url"].get(language)).strip()
            if not listing.startswith("https://"):
                raise StepFailedError(step.id, "aucune fiche choisie")
            state[LISTING_URL] = listing
        case FormFillStep():
            frame = fill_in(step.frame) if step.frame else None
            for form_field in step.fields.values():
                page.fill(
                    fill_in(form_field.selector),
                    fill_in(form_field.value),
                    frame=frame,
                    method=form_field.input,
                    force=form_field.force,
                )
        case ClickStep():
            page.click(fill_in(step.selector), frame=fill_in(step.frame) if step.frame else None)
        case WaitForStep():
            if step.message:
                prompter.show(fill_in(step.message.get(language)))
            if step.js_condition is not None:
                page.wait_for_condition(step.js_condition, timeout_ms=step.timeout_ms)
            else:
                assert step.selector is not None  # noqa: S101 - guaranteed by the schema
                page.wait_for_selector(
                    fill_in(step.selector),
                    frame=fill_in(step.frame) if step.frame else None,
                    timeout_ms=step.timeout_ms,
                )
        case PromptUserStep():
            prompter.pause(fill_in(step.message.get(language)))
        case PromptUserIfPresentStep():
            frame = fill_in(step.frame) if step.frame else None
            if page.is_present(fill_in(step.selector), frame=frame):
                prompter.pause(fill_in(step.message.get(language)))
        case AssertStep():
            _check(step, page, fill_in)
        case CaptureStep():
            path = proof_path.with_name(f"{proof_path.stem}-{len(result.screenshots) + 1}.png")
            page.screenshot(path)
            result.screenshots.append(path)


def _check(step: AssertStep, page: Page, fill_in: "TemplateFiller") -> None:
    failures = []
    selector = fill_in(step.selector) if step.selector else None
    if selector is not None and not page.is_present(selector, frame=None):
        failures.append("élément attendu absent")
    if step.text_contains is not None and fill_in(step.text_contains) not in page.text_of(selector):
        failures.append("texte attendu absent")
    if step.url_matches is not None and not re.search(step.url_matches, page.url()):
        failures.append("adresse de page inattendue")
    if step.js_condition is not None and not page.evaluate(step.js_condition):
        failures.append("condition non remplie")
    if failures:
        raise StepFailedError(step.id, ", ".join(failures))


class TemplateFiller(Protocol):
    def __call__(self, template: str) -> str: ...


# --- verify: is the form still there? -------------------------------------------------------


@dataclass
class VerifyResult:
    checked: list[str] = field(default_factory=list)  # step ids whose element was found
    missing: list[str] = field(default_factory=list)
    stopped_at: str | None = None  # first step that needs the user or changes the page


def verify_form(channel: WebFormChannel, page: Page) -> VerifyResult:
    """Check, without any personal data, that the first elements of the form still exist.

    Navigation is followed; the check stops at the first step that would need personal
    data in a URL, a click, or the user, since the page changes after it.
    """
    result = VerifyResult()
    for step in channel.steps:
        match step:
            case NavigateStep() if step.url is None or "{" not in step.url:
                page.goto(step.url or str(channel.url))
            case WaitForStep() if step.selector and "{" not in step.selector:
                frame = step.frame
                try:
                    page.wait_for_selector(
                        step.selector, frame=frame, timeout_ms=min(step.timeout_ms, 30_000)
                    )
                    result.checked.append(step.id)
                except Exception:
                    result.missing.append(step.id)
            case FormFillStep():
                for name, form_field in step.fields.items():
                    if "{" in form_field.selector:
                        continue
                    found = page.is_present(form_field.selector, frame=step.frame)
                    (result.checked if found else result.missing).append(f"{step.id}.{name}")
            case CaptureStep() | AssertStep():
                continue
            case _:
                result.stopped_at = step.id
                break
    return result
