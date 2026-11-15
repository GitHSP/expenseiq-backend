"""
Encrypted storage for Documents. Each file is encrypted (Fernet: AES with an
integrity check) with a key made from the server's SECRET_KEY, and saved under
a random name in DOCS_DIR, outside the code and never in git. Only its owner
can download it (doc_views.py).

If SECRET_KEY ever changes, files saved before can't be read: keep it as is.
"""
import base64
import hashlib
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

MAX_BYTES = 20 * 1024 * 1024
# What's allowed, checked by the file's first bytes (not its name).
SIGNATURES = [
    (b"%PDF-", "application/pdf"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
]


def docs_dir():
    path = Path(getattr(settings, "DOCS_DIR", "") or os.environ.get("DOCS_DIR", "") or Path(settings.BASE_DIR) / "private_documents")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _fernet():
    key = hashlib.sha256(f"documents:{settings.SECRET_KEY}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def sniff(head):
    """The content type, from the file's first bytes; None if it isn't allowed."""
    for magic, ctype in SIGNATURES:
        if head.startswith(magic):
            return ctype
    if head[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc"):
        return "image/heic"                                   # iPhone photos
    return None


def save(raw):
    """Encrypt and store; returns the stored name."""
    name = secrets.token_hex(24)
    (docs_dir() / name).write_bytes(_fernet().encrypt(raw))
    return name


def load(stored_as):
    try:
        return _fernet().decrypt((docs_dir() / stored_as).read_bytes())
    except (FileNotFoundError, InvalidToken):
        return None


def remove(stored_as):
    try:
        (docs_dir() / stored_as).unlink()
    except FileNotFoundError:
        pass
