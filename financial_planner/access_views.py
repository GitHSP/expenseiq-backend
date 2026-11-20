"""Admin only: every user, and the pages each one can open (financial_planner/access.py)."""
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from .access import DEFAULT_PAGES, PAGES, PageAccess, allowed_pages


def user_json(u):
    return {"id": u.id, "email": u.email, "username": u.username, "is_staff": u.is_staff, "is_active": u.is_active,
            "pages": allowed_pages(u), "joined": u.date_joined.date().isoformat(),
            "is_default": not u.is_staff and not PageAccess.objects.filter(user=u).exists(),
            "last_login": u.last_login.isoformat() if u.last_login else None}


class AdminUsersView(APIView):
    """GET /api/fp/admin/users/ - every user and their pages."""
    permission_classes = [IsAdminUser]

    def get(self, request):
        users = get_user_model().objects.order_by("-is_staff", "email")
        return Response({"pages": PAGES, "default_pages": DEFAULT_PAGES, "users": [user_json(u) for u in users]})


class AdminUserView(APIView):
    """PATCH /api/fp/admin/users/<id>/ {pages: [...] | null (all)}"""
    permission_classes = [IsAdminUser]

    def patch(self, request, pk):
        user = get_user_model().objects.filter(pk=pk).first()
        if not user:
            return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)
        pages = request.data.get("pages")
        if pages is not None and (not isinstance(pages, list) or any(p not in PAGES for p in pages)):
            return Response({"error": f"pages must be a list from: {', '.join(PAGES)}."}, status=status.HTTP_400_BAD_REQUEST)
        PageAccess.objects.update_or_create(user=user, defaults={"pages": pages})
        return Response(user_json(user))
