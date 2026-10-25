"""Playwright implementation of the web form engine's `Page`.

Chromium runs with a persistent profile (cookies set after a human-solved Cloudflare
check are kept between runs) and in a visible window by default, so the user can see
everything and solve CAPTCHAs. Playwright is an optional dependency (`gomme[web]`).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from gomme.brokers.schema import InputMethod
from gomme.privacy import PRIVATE_FILE_MODE, ensure_private_dir

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Locator

DEFAULT_TIMEOUT_MS = 15_000
INSTALL_HINT = (
    "les formulaires web demandent Playwright : uv sync --extra web, "
    "puis uv run playwright install chromium"
)

# React controlled inputs ignore fill(): set the value with the native setter, then fire
# the events React listens to.
_SET_VALUE_JS = """(element, value) => {
    const prototype = Object.getPrototypeOf(element);
    const setter = Object.getOwnPropertyDescriptor(prototype, 'value').set;
    setter.call(element, value);
    element.dispatchEvent(new Event('input', {bubbles: true}));
    element.dispatchEvent(new Event('change', {bubbles: true}));
}"""


class BrowserUnavailableError(RuntimeError):
    pass


class BrowserPage:
    """`Page` backed by a Playwright page, in a persistent context."""

    def __init__(self, context: "BrowserContext") -> None:
        self._context = context
        self._page = context.pages[0] if context.pages else context.new_page()
        self._page.set_default_timeout(DEFAULT_TIMEOUT_MS)

    def _locator(self, selector: str, frame: str | None) -> "Locator":
        if frame:
            return self._page.frame_locator(frame).locator(selector).first
        return self._page.locator(selector).first

    def goto(self, url: str) -> None:
        self._page.goto(url, wait_until="domcontentloaded")

    def fill(
        self, selector: str, value: str, *, frame: str | None, method: InputMethod, force: bool
    ) -> None:
        locator = self._locator(selector, frame)
        locator.wait_for(state="attached" if force else "visible")
        if locator.evaluate("element => element.tagName.toLowerCase()") == "select":
            locator.select_option(value, force=force)
        elif method is InputMethod.TYPE:
            locator.click(force=force)
            locator.press_sequentially(value, delay=20)
        elif method is InputMethod.JS:
            locator.evaluate(_SET_VALUE_JS, value)
        else:
            locator.fill(value, force=force)

    def click(self, selector: str, *, frame: str | None) -> None:
        self._locator(selector, frame).click()

    def wait_for_selector(self, selector: str, *, frame: str | None, timeout_ms: int) -> None:
        self._locator(selector, frame).wait_for(state="attached", timeout=timeout_ms)

    def wait_for_condition(self, js_condition: str, *, timeout_ms: int) -> None:
        self._page.wait_for_function(js_condition, timeout=timeout_ms)

    def is_present(self, selector: str, *, frame: str | None) -> bool:
        return self._locator(selector, frame).count() > 0

    def text_of(self, selector: str | None) -> str:
        if selector is None:
            return self._page.inner_text("body")
        return self._locator(selector, None).text_content() or ""

    def evaluate(self, js_condition: str) -> bool:
        return bool(self._page.evaluate(js_condition))

    def url(self) -> str:
        """The address the user is looking at: the newest tab, if they opened one."""
        for page in reversed(self._context.pages):
            if page.url and page.url != "about:blank":
                return page.url
        return self._page.url

    def screenshot(self, path: Path) -> None:
        ensure_private_dir(path.parent)
        self._page.screenshot(path=str(path), full_page=True)
        path.chmod(PRIVATE_FILE_MODE)  # a proof shows personal data


@contextmanager
def open_browser(profile_dir: Path, *, headless: bool = False) -> Iterator[BrowserPage]:
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        raise BrowserUnavailableError(INSTALL_HINT) from None

    ensure_private_dir(profile_dir)
    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir), headless=headless, viewport=None
            )
        except Error as exc:
            if "Executable doesn't exist" in str(exc):
                raise BrowserUnavailableError(INSTALL_HINT) from None
            raise
        try:
            yield BrowserPage(context)
        finally:
            context.close()
