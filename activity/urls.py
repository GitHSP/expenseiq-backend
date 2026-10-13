from django.urls import path

from . import views

urlpatterns = [
    path("", views.ActivityListView.as_view(), name="activity-list"),
]
