
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