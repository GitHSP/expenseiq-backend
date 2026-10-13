from rest_framework import serializers

from .models import ActivityLog


class ActivityLogSerializer(serializers.ModelSerializer):
    class Meta:
        model  = ActivityLog
        fields = ["id", "created_at", "area", "action", "title", "object_id",
                  "changes", "details", "source", "reason"]
