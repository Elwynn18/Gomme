"""The Playwright adapter against a real (headless) Chromium and a local HTML page.

Opt-in: GOMME_BROWSER_TESTS=1, after `uv run playwright install chromium`. The page is a
local file: no website is ever opened.
"""

import os
from pathlib import Path

import pytest
from fakes import FakePage, FakePrompter

from gomme.brokers.schema import WebFormChannel
from gomme.channels.browser import open_browser
from gomme.channels.web_form import run_steps

pytestmark = pytest.mark.skipif(
    os.environ.get("GOMME_BROWSER_TESTS") != "1", reason="set GOMME_BROWSER_TESTS=1"
)

PAGE = """<!doctype html>
<html><body>
<form id="optout" onsubmit="event.preventDefault(); document.body.dataset.sent = [
    this.name.value, this.email.value, this.state.value, this.react.value,
    document.querySelector('iframe').contentDocument.querySelector('#city').value
  ].join('|'); document.getElementById('done').hidden = false;">
  <input name="name" id="name">
  <input name="email" id="email">
  <select name="state" id="state"><option>AL</option><option>NY</option></select>
  <input name="react" id="react">
  <iframe srcdoc="<input id='city'>"></iframe>
  <button id="submit" type="submit">Send</button>
</form>
<p id="done" hidden>Merci, demande enregistrée</p>
</body></html>
"""


def test_adapter_fills_and_submits_a_local_form(tmp_path: Path) -> None:
    html = tmp_path / "form.html"
    html.write_text(PAGE, encoding="utf-8")
    channel = WebFormChannel.model_validate(
        {
            "type": "web_form",
            "url": "https://example.org/not-used",
            "steps": [
                {"id": "frame", "type": "wait_for", "selector": "#city", "frame": "iframe"},
                {
                    "id": "fill",
                    "type": "form_fill",
                    "fields": {
                        "name": {"selector": "#name", "value": "{full_name}"},
                        "email": {"selector": "#email", "value": "{email}", "input": "type"},
                        "state": {"selector": "#state", "value": "{state}"},
                        "react": {"selector": "#react", "value": "R", "input": "js"},
                    },
                },
                {
                    "id": "city",
                    "type": "form_fill",
                    "frame": "iframe",
                    "fields": {"city": {"selector": "#city", "value": "{city}"}},
                },
                {"id": "send", "type": "click", "selector": "#submit"},
                {"id": "wait", "type": "wait_for", "selector": "#done:not([hidden])"},
                {"id": "check", "type": "assert", "text_contains": "demande enregistrée"},
                {"id": "proof", "type": "capture"},
            ],
        }
    )
    values = {"full_name": "Jane Doe", "email": "jane@example.org", "state": "NY", "city": "X"}

    with open_browser(tmp_path / "profile", headless=True) as page:
        page.goto(html.as_uri())
        result = run_steps(
            channel,
            page,
            FakePrompter(FakePage()),  # no pause in these steps
            values,
            language="fr",
            proof_path=tmp_path / "proof.png",
        )
        assert page.evaluate(
            "() => document.body.dataset.sent === 'Jane Doe|jane@example.org|NY|R|X'"
        )

    assert result.warnings == []
    (proof,) = result.screenshots
    assert proof.read_bytes().startswith(b"\x89PNG")
    assert (proof.stat().st_mode & 0o777) == 0o600
