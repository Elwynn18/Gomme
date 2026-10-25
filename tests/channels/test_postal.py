"""Tests for PDF letters (postal channel)."""

import re
from datetime import date
from pathlib import Path

import pytest

from gomme.brokers.registry import load_broker_file
from gomme.brokers.schema import Broker
from gomme.channels.postal import (
    LINE_CHARS,
    LINES_PER_PAGE,
    Letter,
    LetterError,
    compose_letter,
    render_pdf,
)
from gomme.compose import compose
from gomme.profile import MissingProfileDataError, Profile

TODAY = date(2026, 10, 26)


@pytest.fixture
def broker(fixtures_brokers_dir: Path) -> Broker:
    return load_broker_file(fixtures_brokers_dir / "fr" / "courtier-fictif.yml")


def letter(broker: Broker, profile: Profile) -> Letter:
    return compose_letter(broker, profile, compose(broker, profile), TODAY)


def test_letter_layout(broker: Broker, profile: Profile) -> None:
    lines = letter(broker, profile).lines

    assert lines[:4] == (
        "Jeanne Exemple",
        "1 rue de l'Exemple",
        "00000 Nulle-Part",
        "jeanne.exemple@example.org",
    )
    recipient = [line.strip() for line in lines if line.startswith(" " * 34)]
    # The address already starts with the broker's name: it is not repeated.
    assert recipient[:2] == ["Courtier Fictif", "Service DPO"]
    assert "Nulle-Part, le 26/10/2026" in recipient
    assert "Lettre recommandée avec accusé de réception" in lines
    assert any(line.startswith("Objet : Demande d'effacement") for line in lines)
    assert lines[-1] == "Jeanne Exemple"
    assert all(len(line) <= LINE_CHARS for line in lines)


def test_list_items_get_a_hanging_indent(broker: Broker, profile: Profile) -> None:
    long_street = profile.postal_address.model_copy(  # type: ignore[union-attr]
        update={"street": "Résidence des Exemples, bâtiment C, escalier 4, 1 rue de l'Exemple"}
    )
    lines = letter(broker, profile.model_copy(update={"postal_address": long_street})).lines

    index = next(i for i, line in enumerate(lines) if line.startswith("- Adresse postale"))
    assert lines[index + 1].startswith("  ")


def test_letter_needs_both_addresses(broker: Broker, profile: Profile) -> None:
    with pytest.raises(MissingProfileDataError):
        compose_letter(
            broker,
            profile.model_copy(update={"postal_address": None}),
            compose(broker, profile),
            TODAY,
        )
    email_only = broker.model_copy(update={"channels": broker.channels[:1]})
    with pytest.raises(LetterError, match="adresse postale"):
        compose_letter(email_only, profile, compose(broker, profile), TODAY)


def test_pdf_structure_is_valid() -> None:
    pdf = render_pdf(Letter(("Bonjour (test) \\ é œ €",)), title="Titre")

    assert pdf.startswith(b"%PDF-1.4\n")
    assert pdf.endswith(b"%%EOF\n")
    startxref = int(pdf.rsplit(b"startxref\n", 1)[1].split(b"\n")[0])
    assert pdf[startxref:].startswith(b"xref\n")
    offsets = re.findall(rb"(\d{10}) 00000 n ", pdf[startxref:])
    for number, offset in enumerate(offsets, start=1):
        assert pdf[int(offset) :].startswith(f"{number} 0 obj".encode())
    # Parentheses and backslashes escaped; accents in WinAnsi (cp1252).
    assert b"(Bonjour \\(test\\) \\\\ \xe9 \x9c \x80) Tj" in pdf
    assert b"/Encoding /WinAnsiEncoding" in pdf


def test_long_letters_span_several_pages() -> None:
    pdf = render_pdf(Letter(tuple(f"ligne {n}" for n in range(LINES_PER_PAGE * 2 + 1))))

    assert b"/Count 3" in pdf
    assert pdf.count(b"/Type /Page ") == 3


def test_letters_print_names_outside_winansi_without_question_marks(
    broker: Broker, profile: Profile
) -> None:
    polish = profile.model_copy(update={"first_name": "Łukasz", "last_name": "Wójcik Żółta"})

    result = letter(broker, polish)

    assert result.lines[0] == "Lukasz Wójcik Zólta"
    assert set(result.transliterated) == {"Ł", "ł", "Ż"}
    assert b"?" not in render_pdf(result).split(b"stream")[1]


def test_unprintable_names_stop_the_letter(broker: Broker, profile: Profile) -> None:
    greek = profile.model_copy(update={"last_name": "Παπαδόπουλος"})

    with pytest.raises(LetterError, match="impossibles à imprimer"):
        letter(broker, greek)


def test_letter_omits_the_email_when_the_broker_does_not_need_it(
    broker: Broker, profile: Profile
) -> None:
    name_only = broker.model_copy(update={"required_fields": ("full_name", "postal_address")})

    lines = compose_letter(name_only, profile, compose(name_only, profile), TODAY).lines

    assert not any("example.org" in line for line in lines)


def test_decomposed_accents_are_printed(broker: Broker, profile: Profile) -> None:
    nfd = profile.model_copy(update={"first_name": "Hélène"})

    result = letter(broker, nfd)

    assert result.lines[0].startswith("Hélène")
    assert result.transliterated == ()
