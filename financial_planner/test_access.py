from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient


class PageAccessTests(TestCase):
    def setUp(self):
        U = get_user_model()
        self.admin = U.objects.create_user(email="ad@example.test", username="ad", password="x-Test-12345", is_staff=True)
        self.friend = U.objects.create_user(email="fr@example.test", username="fr", password="x-Test-12345")
        self.a, self.f = APIClient(), APIClient()
        self.a.force_authenticate(self.admin)
        self.f.force_authenticate(self.friend)

    def test_admin_sets_pages_and_the_user_sees_them(self):
        self.assertEqual(self.f.get("/api/auth/me/").data["pages"],                       # the default four
                         ["dashboard", "expenses", "analytics", "payments"])
        r = self.a.patch(f"/api/fp/admin/users/{self.friend.id}/", {"pages": ["dashboard", "payments"]}, format="json")
        self.assertEqual(r.data["pages"], ["dashboard", "payments"])
        me = self.f.get("/api/auth/me/").data
        self.assertEqual((me["pages"], me["is_staff"]), (["dashboard", "payments"], False))
        self.assertEqual(self.a.patch(f"/api/fp/admin/users/{self.friend.id}/", {"pages": ["nope"]}, format="json").status_code, 400)
        self.a.patch(f"/api/fp/admin/users/{self.friend.id}/", {"pages": None}, format="json")
        self.assertIsNone(self.f.get("/api/auth/me/").data["pages"])

    def test_only_admins_and_admins_see_everything(self):
        self.assertEqual(self.f.get("/api/fp/admin/users/").status_code, 403)
        self.assertEqual(self.f.patch(f"/api/fp/admin/users/{self.admin.id}/", {"pages": []}, format="json").status_code, 403)
        self.assertEqual(len(self.a.get("/api/fp/admin/users/").data["users"]), 2)
        self.a.patch(f"/api/fp/admin/users/{self.admin.id}/", {"pages": []}, format="json")
        self.assertIsNone(self.a.get("/api/auth/me/").data["pages"])                      # staff: never locked out

    @override_settings(ANTHROPIC_API_KEY="")
    def test_aurora_off_by_default(self):
        r = self.f.post("/api/assistant/chat/", {"messages": [{"role": "user", "content": "hi"}]}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.a.post("/api/assistant/chat/", {"messages": [{"role": "user", "content": "hi"}]},
                                     format="json").status_code, 200)            # the admin always has it

    @override_settings(ANTHROPIC_API_KEY="")
    def test_aurora_blocked_on_the_server(self):
        self.a.patch(f"/api/fp/admin/users/{self.friend.id}/", {"pages": ["dashboard"]}, format="json")
        r = self.f.post("/api/assistant/chat/", {"messages": [{"role": "user", "content": "hi"}]}, format="json")
        self.assertEqual(r.status_code, 403)
        self.a.patch(f"/api/fp/admin/users/{self.friend.id}/", {"pages": ["dashboard", "aurora"]}, format="json")
        self.assertEqual(self.f.post("/api/assistant/chat/", {"messages": [{"role": "user", "content": "hi"}]},
                                     format="json").status_code, 200)
