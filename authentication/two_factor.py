# authentication/two_factor.py
#
# Authenticator-app two-factor login (TOTP, RFC 6238 — the 6-digit codes
# from Google Authenticator, Microsoft Authenticator, 1Password, etc.).
#
# Login with 2FA on happens in two steps: the password step returns a
# short-lived signed "mfa_token" instead of tokens, and the code step trades
# that plus a valid code for the usual JWTs.

import base64
import hashlib
import hmac
import secrets
import time

import pyotp
import segno
from cryptography.fernet import Fernet
from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.utils import timezone

from .models import TwoFactor

ISSUER = "Glacier"
MFA_TOKEN_SALT = "glacier.mfa-login"
MFA_TOKEN_MAX_AGE = 5 * 60          # seconds to enter the code after the password
CODE_MAX_FAILURES = 5
CODE_LOCKOUT_SECONDS = 15 * 60
BACKUP_CODE_COUNT = 10


# ── Secret storage ──

def _fernet():
    key = hashlib.sha256(f"two-factor:{settings.SECRET_KEY}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(secret):
    return _fernet().encrypt(secret.encode()).decode()


def decrypt_secret(token):
    return _fernet().decrypt(token.encode()).decode()


def _hash_backup_code(code):
    normalised = code.replace("-", "").replace(" ", "").lower()
    return hmac.new(settings.SECRET_KEY.encode(), normalised.encode(), hashlib.sha256).hexdigest()


def new_backup_codes():
    """Return (codes to show once, hashes to store)."""
    codes = [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(BACKUP_CODE_COUNT)]
    return codes, [_hash_backup_code(c) for c in codes]


# ── Setup ──

def is_enabled(user):
    tf = getattr(user, "two_factor", None)
    return bool(tf and tf.enabled)


def start_setup(user):
    """Create (or replace) a pending secret; return what the app shows."""
    secret = pyotp.random_base32()
    TwoFactor.objects.update_or_create(
        user=user,
        defaults={"secret_encrypted": encrypt_secret(secret), "enabled": False,
                  "last_used_step": 0, "backup_code_hashes": [], "enabled_at": None},
    )
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name=ISSUER)
    qr = segno.make(uri, error="m")
    return {
        "secret": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
        "otpauth_uri": uri,
        "qr_svg": qr.svg_data_uri(scale=5, border=2, dark="#0d0d0d", light="#ffffff"),
    }


def _lock_key(user):
    return f"mfa-failures:{user.pk}"


def is_locked(user):
    return cache.get(_lock_key(user), 0) >= CODE_MAX_FAILURES


def _record_failure(user):
    cache.add(_lock_key(user), 0, CODE_LOCKOUT_SECONDS)
    cache.incr(_lock_key(user))


def verify(user, code, allow_backup=True):
    """
    Check a 6-digit code (or, if allowed, a backup code). Each time step can
    be used once and wrong guesses count towards a lockout. Returns
    (ok, used_backup_code).
    """
    tf = getattr(user, "two_factor", None)
    if tf is None or is_locked(user):
        return False, False
    code = (code or "").strip().replace(" ", "")

    if code.isdigit() and len(code) == 6:
        totp = pyotp.TOTP(decrypt_secret(tf.secret_encrypted))
        now = int(time.time())
        for offset in (-1, 0, 1):        # allow ±30s of clock drift
            step = (now // 30) + offset
            if step > tf.last_used_step and hmac.compare_digest(totp.at(step * 30), code):
                tf.last_used_step = step
                tf.save(update_fields=["last_used_step"])
                cache.delete(_lock_key(user))
                return True, False
    elif allow_backup and len(code.replace("-", "")) == 12:
        hashed = _hash_backup_code(code)
        if hashed in tf.backup_code_hashes:
            tf.backup_code_hashes = [h for h in tf.backup_code_hashes if h != hashed]
            tf.save(update_fields=["backup_code_hashes"])
            cache.delete(_lock_key(user))
            return True, True

    _record_failure(user)
    return False, False


def confirm_setup(user, code):
    """Turn 2FA on once the user proves their app works. Returns backup codes."""
    tf = getattr(user, "two_factor", None)
    if tf is None or tf.enabled:
        return None
    ok, _ = verify(user, code, allow_backup=False)
    if not ok:
        return None
    codes, hashes = new_backup_codes()
    tf.enabled = True
    tf.enabled_at = timezone.now()
    tf.backup_code_hashes = hashes
    tf.save(update_fields=["enabled", "enabled_at", "backup_code_hashes"])
    return codes


# ── Login step tokens ──

def make_mfa_token(user):
    return signing.dumps({"uid": user.pk}, salt=MFA_TOKEN_SALT)


def read_mfa_token(token):
    try:
        return signing.loads(token or "", salt=MFA_TOKEN_SALT, max_age=MFA_TOKEN_MAX_AGE)["uid"]
    except (signing.BadSignature, signing.SignatureExpired, KeyError, TypeError):
        return None


