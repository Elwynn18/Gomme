"""Postal channel: the request as a printable PDF letter.

Some French brokers only accept letters. The PDF is written by a small generator rather
than a dependency: one standard font (Courier, built into every PDF reader, so nothing is
embedded), WinAnsi encoding for French accents, A4 pages. Courier is monospaced, so
lines can be wrapped exactly without font metrics.
"""

import textwrap
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from gomme.brokers.fields import ProfileField
from gomme.brokers.schema import Broker, PostalChannel
from gomme.compose import RequestText
from gomme.profile import MissingProfileDataError, Profile

PAGE_WIDTH, PAGE_HEIGHT = 595, 842  # A4, in points
MARGIN = 64
FONT_SIZE = 10.5
LEADING = 14
CHAR_WIDTH = 0.6 * FONT_SIZE  # every Courier glyph is 600/1000 em wide
LINE_CHARS = int((PAGE_WIDTH - 2 * MARGIN) / CHAR_WIDTH)
LINES_PER_PAGE = int((PAGE_HEIGHT - 2 * MARGIN) / LEADING)
RECIPIENT_INDENT = 34  # characters: the recipient block sits on the right


class LetterError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Letter:
    lines: tuple[str, ...]
    transliterated: tuple[str, ...] = ()  # characters printed without their accent


# Letters with no decomposition into a base letter plus accents.
_TRANSLITERATIONS = {"Ł": "L", "ł": "l", "Đ": "D", "đ": "d", "Ħ": "H", "ħ": "h", "\u0131": "i"}


def to_winansi(text: str) -> tuple[str, set[str]]:
    """`text` with every character the PDF font can print, and the ones transliterated.

    Raises LetterError for characters with no Latin equivalent (Greek, Cyrillic...): a
    letter must never silently print "?" instead of a name.
    """
    printed: list[str] = []
    replaced: set[str] = set()
    impossible: set[str] = set()
    # NFC first: an accent typed as a separate combining mark ("e" + U+0301) becomes "é".
    for char in unicodedata.normalize("NFC", text).translate(_REPLACEMENTS):
        try:
            char.encode("cp1252")
            printed.append(char)
            continue
        except UnicodeEncodeError:
            pass
        base = _TRANSLITERATIONS.get(char) or "".join(
            part for part in unicodedata.normalize("NFKD", char) if not unicodedata.combining(part)
        )
        try:
            base.encode("cp1252")
        except UnicodeEncodeError:
            base = ""
        if base:
            printed.append(base)
            replaced.add(char)
        else:
            impossible.add(char)
    if impossible:
        raise LetterError(
            "caractères impossibles à imprimer dans le courrier : " + " ".join(sorted(impossible))
        )
    return "".join(printed), replaced


def postal_address(broker: Broker) -> str | None:
    for channel in broker.channels:
        if isinstance(channel, PostalChannel):
            return channel.address
    return None


def compose_letter(broker: Broker, profile: Profile, text: RequestText, today: date) -> Letter:
    """Lay out a letter: sender, recipient, place and date, subject, body."""
    recipient = postal_address(broker)
    if recipient is None:
        raise LetterError(f"{broker.slug} n'a pas d'adresse postale")
    if profile.postal_address is None:
        raise MissingProfileDataError([ProfileField.POSTAL_ADDRESS])
    address = profile.postal_address
    french = text.language == "fr"

    lines: list[str] = [
        profile.full_name,
        address.street,
        f"{address.postal_code} {address.city}",
        *([address.state] if address.state else []),
        # Minimisation: the email appears only if the broker needs it.
        *([profile.email] if ProfileField.EMAIL in broker.required_fields else []),
        "",
    ]
    recipient_parts = [part.strip() for part in recipient.split(",") if part.strip()]
    if not broker.name.lower().startswith(recipient_parts[0].lower()):
        recipient_parts.insert(0, broker.name)  # the address does not start with the name
    width = LINE_CHARS - RECIPIENT_INDENT
    for part in recipient_parts:
        lines += [" " * RECIPIENT_INDENT + row for row in _wrap(part, width)]
    when = (
        f"{address.city}, le {today.strftime('%d/%m/%Y')}"
        if french
        else f"{address.city}, {today.isoformat()}"
    )
    lines += ["", " " * RECIPIENT_INDENT + when, ""]
    lines.append(
        "Lettre recommandée avec accusé de réception"
        if french
        else "Sent by registered mail with acknowledgement of receipt"
    )
    lines += [""]
    lines += _wrap(("Objet : " if french else "Subject: ") + text.subject, LINE_CHARS)
    lines += [""]
    for paragraph in text.body.rstrip("\n").split("\n"):
        lines += _wrap(paragraph, LINE_CHARS) if paragraph.strip() else [""]
    printable, replaced = zip(*(to_winansi(line) for line in lines), strict=True)
    return Letter(tuple(printable), tuple(sorted(set().union(*replaced))))


def _wrap(text: str, width: int) -> list[str]:
    indent = text[: len(text) - len(text.lstrip())]
    if text.lstrip().startswith("- "):
        indent += "  "  # hanging indent under list items
    return textwrap.wrap(text, width, subsequent_indent=indent, break_on_hyphens=False) or [""]


# --- PDF writing ---------------------------------------------------------------------------

_REPLACEMENTS = str.maketrans({"\u202f": " ", "\u00a0": " ", "\u2011": "-"})


def _pdf_string(line: str) -> bytes:
    encoded = to_winansi(line)[0].encode("cp1252")
    return b"(" + encoded.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def _page_stream(lines: Sequence[str]) -> bytes:
    top = PAGE_HEIGHT - MARGIN - FONT_SIZE
    parts = [b"BT", f"/F1 {FONT_SIZE} Tf {LEADING} TL {MARGIN} {top} Td".encode()]
    for line in lines:
        parts.append(_pdf_string(line) + b" Tj T*")
    parts.append(b"ET")
    return b"\n".join(parts)


def render_pdf(letter: Letter, *, title: str = "") -> bytes:
    pages = [
        letter.lines[start : start + LINES_PER_PAGE]
        for start in range(0, max(len(letter.lines), 1), LINES_PER_PAGE)
    ]
    # Objects: 1 catalog, 2 page tree, 3 font, 4 info, then a (page, content) pair per page.
    first_page = 5
    page_ids = [first_page + 2 * index for index in range(len(pages))]
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            b"<< /Type /Pages /Kids ["
            + b" ".join(f"{page} 0 R".encode() for page in page_ids)
            + f"] /Count {len(pages)} >>".encode()
        ),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>",
        4: b"<< /Producer (Gomme) /Title " + _pdf_string(title) + b" >>",
    }
    for page_id, lines in zip(page_ids, pages, strict=True):
        stream = _page_stream(lines)
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>"
        ).encode()
        objects[page_id + 1] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        )

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(output)
        output += f"{number} 0 obj\n".encode() + objects[number] + b"\nendobj\n"
    xref = len(output)
    count = max(objects) + 1
    output += f"xref\n0 {count}\n0000000000 65535 f \n".encode()
    for number in range(1, count):
        output += f"{offsets[number]:010d} 00000 n \n".encode()
    output += (
        f"trailer\n<< /Size {count} /Root 1 0 R /Info 4 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(output)
