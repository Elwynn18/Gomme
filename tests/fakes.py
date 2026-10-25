"""Test doubles shared by several test modules."""

import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from gomme.brokers.schema import InputMethod


class FakeSmtp:
    """Records what Gomme does; can fail on demand."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[str] = []
        self.sent: list[tuple[EmailMessage, list[str] | None]] = []
        self.fail_on = fail_on
        self.refuse: set[str] = set()  # recipients the server rejects at RCPT

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if name == self.fail_on:
            raise OSError("simulated failure")

    def ehlo(self) -> None:
        self._record("ehlo")

    def starttls(self, *, context: ssl.SSLContext) -> None:
        assert context.verify_mode is ssl.CERT_REQUIRED
        self._record("starttls")

    def login(self, user: str, password: str) -> None:
        self._record(f"login:{user}")

    def send_message(
        self, msg: EmailMessage, from_addr: str | None = None, to_addrs: list[str] | None = None
    ) -> dict[str, tuple[int, bytes]]:
        self._record("send")
        refused = {addr: (550, b"no such user") for addr in to_addrs or [] if addr in self.refuse}
        if refused and len(refused) == len(to_addrs or []):
            raise smtplib.SMTPRecipientsRefused(refused)
        self.sent.append((msg, to_addrs))
        return refused

    def quit(self) -> None:
        self._record("quit")


class FakePage:
    """Elements present on the page, and a log of what the engine did."""

    def __init__(self, present: set[str] | None = None, url_after_pause: str | None = None):
        self.present = present or set()
        self.log: list[tuple[str, ...]] = []
        self.current_url = ""
        self.url_after_pause = url_after_pause
        self.failing: set[str] = set()

    def _maybe_fail(self, selector: str) -> None:
        if selector in self.failing:
            raise TimeoutError(f"Timeout 15000ms exceeded waiting for {selector}")

    def goto(self, url: str) -> None:
        self.log.append(("goto", url))
        self.current_url = url

    def fill(
        self, selector: str, value: str, *, frame: str | None, method: InputMethod, force: bool
    ) -> None:
        self._maybe_fail(selector)
        self.log.append(("fill", selector, value, method.value, str(frame)))

    def click(self, selector: str, *, frame: str | None) -> None:
        self._maybe_fail(selector)
        self.log.append(("click", selector))

    def wait_for_selector(self, selector: str, *, frame: str | None, timeout_ms: int) -> None:
        self._maybe_fail(selector)
        if selector not in self.present:
            raise TimeoutError(f"Timeout {timeout_ms}ms exceeded")
        self.log.append(("wait", selector))

    def wait_for_condition(self, js_condition: str, *, timeout_ms: int) -> None:
        self.log.append(("wait_js", js_condition))

    def is_present(self, selector: str, *, frame: str | None) -> bool:
        return selector in self.present

    def text_of(self, selector: str | None) -> str:
        return "Merci, votre demande est enregistrée"

    def evaluate(self, js_condition: str) -> bool:
        return True

    def url(self) -> str:
        return self.current_url

    def screenshot(self, path: Path) -> None:
        self.log.append(("screenshot", path.name))


class FakePrompter:
    def __init__(self, page: FakePage, answers: list[str] | None = None) -> None:
        self.page = page
        self.answers = answers or []
        self.shown: list[str] = []
        self.pauses: list[str] = []

    def show(self, message: str) -> None:
        self.shown.append(message)

    def pause(self, message: str) -> None:
        self.pauses.append(message)
        if self.page.url_after_pause:
            self.page.current_url = self.page.url_after_pause

    def ask(self, message: str) -> str:
        return self.answers.pop(0)
