import time
from unittest import mock

import pyotp
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from . import two_factor

User = get_user_model()
PASSWORD = "Str0ng-Passphrase-41"


class TwoFactorTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(email="tf@example.test", username="tf", password=PASSWORD)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def secret(self):
        self.user.refresh_from_db()
        return two_factor.decrypt_secret(self.user.two_factor.secret_encrypted)

    def code(self, at=None):
        return pyotp.TOTP(self.secret()).at(at or time.time())

    def enable(self):
        setup = self.client.post("/api/auth/2fa/").data
        res = self.client.put("/api/auth/2fa/", {"code": self.code()}, format="json")
        self.assertEqual(res.status_code, 200)
        return setup, res.data["backup_codes"]

    def password_login(self):
        return APIClient().post("/api/auth/login/", {"email": "tf@example.test", "password": PASSWORD}, format="json")

    # ── Setup ──
    def test_setup_returns_qr_and_is_not_active_until_confirmed(self):
        setup = self.client.post("/api/auth/2fa/").data
        self.assertTrue(setup["qr_svg"].startswith("data:image/svg+xml"))
        self.assertIn("otpauth://totp/Glacier:tf%40example.test", setup["otpauth_uri"])
        self.assertFalse(self.client.get("/api/auth/2fa/").data["enabled"])
        self.assertIn("access_token", self.password_login().data)  # still password-only

    def test_secret_is_encrypted_at_rest(self):
        setup = self.client.post("/api/auth/2fa/").data
        stored = User.objects.get(pk=self.user.pk).two_factor.secret_encrypted
        self.assertNotIn(setup["secret"].replace(" ", ""), stored)

    def test_wrong_code_does_not_enable(self):
        self.client.post("/api/auth/2fa/")
        res = self.client.put("/api/auth/2fa/", {"code": "000000"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertFalse(self.client.get("/api/auth/2fa/").data["enabled"])

    def test_enable_gives_ten_backup_codes(self):
        _, codes = self.enable()
        self.assertEqual(len(codes), 10)
        self.assertEqual(self.client.get("/api/auth/2fa/").data["backup_codes_left"], 10)
        self.assertEqual(self.client.post("/api/auth/2fa/").status_code, 400)  # can't restart while on

    # ── Login ──
    def test_password_alone_no_longer_logs_in(self):
        self.enable()
        res = self.password_login()
        self.assertTrue(res.data["mfa_required"])
        self.assertNotIn("access_token", res.data)

    def test_code_completes_login(self):
        self.enable()
        token = self.password_login().data["mfa_token"]
        res = APIClient().post("/api/auth/login/2fa/", {"mfa_token": token, "code": self.code(time.time() + 30)}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertIn("access_token", res.data)

    def test_a_code_cannot_be_replayed(self):
        self.enable()
        token = self.password_login().data["mfa_token"]
        code = self.code(time.time() + 30)
        self.assertEqual(APIClient().post("/api/auth/login/2fa/", {"mfa_token": token, "code": code}, format="json").status_code, 200)
        self.assertEqual(APIClient().post("/api/auth/login/2fa/", {"mfa_token": token, "code": code}, format="json").status_code, 401)

    def test_backup_code_works_once(self):
        _, codes = self.enable()
        token = self.password_login().data["mfa_token"]
        res = APIClient().post("/api/auth/login/2fa/", {"mfa_token": token, "code": codes[0].upper()}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["backup_codes_left"], 9)
        res = APIClient().post("/api/auth/login/2fa/", {"mfa_token": token, "code": codes[0]}, format="json")
        self.assertEqual(res.status_code, 401)

    def test_wrong_codes_lock_out(self):
        self.enable()
        token = self.password_login().data["mfa_token"]
        for _ in range(5):
            APIClient().post("/api/auth/login/2fa/", {"mfa_token": token, "code": "123456"}, format="json")
        res = APIClient().post("/api/auth/login/2fa/", {"mfa_token": token, "code": self.code(time.time() + 30)}, format="json")
        self.assertEqual(res.status_code, 429)

    def test_tampered_or_expired_mfa_token_is_rejected(self):
        self.enable()
        token = self.password_login().data["mfa_token"]
        res = APIClient().post("/api/auth/login/2fa/", {"mfa_token": token[:-2] + "xx", "code": self.code()}, format="json")
        self.assertEqual(res.status_code, 401)
        with mock.patch("django.core.signing.time.time", return_value=time.time() + 400):
            res = APIClient().post("/api/auth/login/2fa/", {"mfa_token": token, "code": self.code()}, format="json")
        self.assertEqual(res.status_code, 401)

    def test_mfa_token_for_one_user_cannot_unlock_another(self):
        self.enable()
        other = User.objects.create_user(email="o@example.test", username="o", password=PASSWORD)
        token = two_factor.make_mfa_token(other)   # other has no 2FA → no session from this endpoint
        res = APIClient().post("/api/auth/login/2fa/", {"mfa_token": token, "code": self.code()}, format="json")
        self.assertEqual(res.status_code, 401)

    # ── Turning it off ──
    def test_disable_needs_password_and_code(self):
        self.enable()
        later = time.time() + 30
        self.assertEqual(self.client.delete("/api/auth/2fa/", {"password": "wrong", "code": self.code(later)}, format="json").status_code, 400)
        self.assertEqual(self.client.delete("/api/auth/2fa/", {"password": PASSWORD, "code": "000000"}, format="json").status_code, 400)
        self.assertEqual(self.client.delete("/api/auth/2fa/", {"password": PASSWORD, "code": self.code(later)}, format="json").status_code, 200)
        self.assertIn("access_token", self.password_login().data)

    def test_regenerating_backup_codes_invalidates_old_ones(self):
        _, old = self.enable()
        res = self.client.post("/api/auth/2fa/backup-codes/", {"code": self.code(time.time() + 30)}, format="json")
        self.assertEqual(len(res.data["backup_codes"]), 10)
        token = self.password_login().data["mfa_token"]
        res = APIClient().post("/api/auth/login/2fa/", {"mfa_token": token, "code": old[1]}, format="json")
        self.assertEqual(res.status_code, 401)
