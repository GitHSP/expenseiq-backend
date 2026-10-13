from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ActivityLog
from .serializers import ActivityLogSerializer


class ActivityListView(APIView):
    """
    GET /api/activity/?area=debt&object_id=3&before=<id>&limit=50
    Newest first. `before` pages backwards: pass the last id you received.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = ActivityLog.objects.filter(user=request.user)
        area = request.query_params.get("area")
        if area:
            qs = qs.filter(area=area)
        object_id = request.query_params.get("object_id")
        if object_id and object_id.isdigit():
            qs = qs.filter(object_id=int(object_id))
        before = request.query_params.get("before")
        if before and before.isdigit():
            qs = qs.filter(id__lt=int(before))
        try:
            limit = max(1, min(int(request.query_params.get("limit", 50)), 200))
        except ValueError:
            limit = 50
        items = list(qs.order_by("-id")[: limit + 1])
        return Response({
            "results":  ActivityLogSerializer(items[:limit], many=True).data,
            "has_more": len(items) > limit,
        })
