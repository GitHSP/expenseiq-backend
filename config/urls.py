from django.contrib import admin
from django.urls    import path, include

urlpatterns = [
    path('admin/',       admin.site.urls),
    path('api/auth/',    include('authentication.urls')),
    path('api/',         include('expenses.urls')),
    path('api/fp/',      include('financial_planner.urls')),  # ← add this
    path('api/assistant/', include('assistant.urls')),
]