"""Masking of personal data before it reaches a screen, a log or an error message."""

import os
import stat
from pathlib import Path

MASK = "***"
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700


def mask_text(value: str) -> str:
    """``Dupont`` -> ``D***``; empty stays empty."""
    value = value.strip()
    return f"{value[0]}{MASK}" if value else ""


def mask_email(address: str) -> str:
    """``jean.dupont@exemple.fr`` -> ``j***@e***.fr`` (the top-level domain is kept)."""
    local, at, domain = address.strip().partition("@")
    if not at:
        return mask_text(local)
    name, dot, tld = domain.rpartition(".")
    if not dot:
        return f"{mask_text(local)}@{mask_text(domain)}"
    return f"{mask_text(local)}@{mask_text(name)}.{tld}"


def mask_phone(number: str) -> str:
    """Only the last two digits stay: ``06 12 34 56 78`` -> ``***78``."""
    digits = [char for char in number if char.isdigit()]
    return f"{MASK}{''.join(digits[-2:])}" if digits else ""


def write_private_file(path: Path, text: str) -> None:
    """Write `text` to a file only its owner can read (mode 600), even while it is created."""
    write_private_bytes(path, text.encode("utf-8"))


def write_private_bytes(path: Path, data: bytes) -> None:
    """Binary variant of write_private_file (PDF letters...)."""
    ensure_private_dir(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    path.chmod(PRIVATE_FILE_MODE)  # an existing file keeps its mode through os.open


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)


def is_private(path: Path) -> bool:
    """False if the group or others can read or write the file."""
    return not path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)
