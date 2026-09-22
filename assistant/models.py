# assistant/models.py
#
# Phase 1 stored nothing server-side — chat lived only in the browser's
# IndexedDB. Phase 2 adds optional cross-device sync, and the server only
# ever sees ciphertext:
#
#   - EncryptionKey: the user's Data Encryption Key (DEK), wrapped by a
#     Key Encryption Key (KEK) derived from a sync passphrase the user
#     chooses on-device. That passphrase is NEVER sent here — without it,
#     this row is useless (PBKDF2 + AES-GCM, both in the browser).
#   - SyncedMessage: AES-GCM ciphertext blobs, one per chat message. The
#     plaintext, and the key that could decrypt it, never leave the
#     browser unless the user also knows the sync passphrase.
#
# A user who never turns sync on never has a row in either table.

from django.conf import settings
from django.db import models


class EncryptionKey(models.Model):
    """One wrapped DEK per user — written the first time any device turns
    sync on; read by every other device that joins that sync with the
    matching passphrase."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="assistant_encryption_key"
    )
    wrapped_dek = models.TextField()           # base64 AES-GCM ciphertext of the DEK
    wrap_iv = models.CharField(max_length=64)  # base64 IV used to wrap it
    salt = models.CharField(max_length=64)     # base64 PBKDF2 salt
    kdf_iterations = models.PositiveIntegerField(default=210_000)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Sync key for {self.user.email}"


class SyncedMessage(models.Model):
    """
    One encrypted chat message. `client_id` is a UUID the browser assigns
    when the message is first created locally, so any device can push or
    pull without creating duplicates or needing server-assigned IDs to
    round-trip back into IndexedDB.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="assistant_synced_messages"
    )
    client_id = models.CharField(max_length=64)
    ciphertext = models.TextField()        # base64 AES-GCM ciphertext
    iv = models.CharField(max_length=64)   # base64 IV, unique per message
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ["user", "client_id"]
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.user.email} — message {self.client_id}"
