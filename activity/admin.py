from django.contrib import admin

from .models import ActivityLog


@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = ["created_at", "user", "area", "action", "title", "source"]
    list_filter  = ["area", "action", "source"]
    search_fields = ["title", "reason"]
