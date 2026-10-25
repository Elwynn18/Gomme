"""Tests for masking and private files."""

import stat
from pathlib import Path

import pytest

from gomme.privacy import is_private, mask_email, mask_phone, mask_text, write_private_file


@pytest.mark.parametrize(
    ("address", "masked"),
    [
        ("jean.dupont@exemple.fr", "j***@e***.fr"),
        ("a@b.example.org", "a***@b***.org"),
        ("pas-une-adresse", "p***"),
        ("x@localhost", "x***@l***"),
    ],
)
def test_mask_email(address: str, masked: str) -> None:
    assert mask_email(address) == masked


def test_mask_text_and_phone() -> None:
    assert mask_text("Dupont") == "D***"
    assert mask_text("  ") == ""
    assert mask_phone("06 12 34 56 78") == "***78"
    assert mask_phone("") == ""


def test_private_file_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "secret.yml"

    write_private_file(path, "x: 1\n")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert is_private(path)


def test_existing_readable_file_becomes_private(tmp_path: Path) -> None:
    path = tmp_path / "secret.yml"
    path.write_text("old", encoding="utf-8")
    path.chmod(0o644)
    assert not is_private(path)

    write_private_file(path, "new")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="utf-8") == "new"
