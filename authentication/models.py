
# authentication/models.py

from django.contrib.auth.models import AbstractUser
from django.db import models

class User(AbstractUser):
    """
    Custom user model — extends Django's built in User.
    We use email as the login field instead of username.
    """
    email = models.EmailField(unique=True)

    # Use email to login instead of username
    USERNAME_FIELD  = 'email'
    REQUIRED_FIELDS = ['username']

    def __str__(self):
        return self.email

class TwoFactor(models.Model):
    """
    Authenticator-app (TOTP) two-factor login for a user. The shared secret
    is stored encrypted (see two_factor.py); backup codes are stored only as
    keyed hashes and each can be used once.
    """
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="two_factor")
    secret_encrypted = models.TextField()
    enabled = models.BooleanField(default=False)          # False while setup is unconfirmed
    last_used_step = models.BigIntegerField(default=0)    # stops a code being replayed
    backup_code_hashes = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    enabled_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"2FA for {self.user} ({'on' if self.enabled else 'pending'})"
