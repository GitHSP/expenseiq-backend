from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from . import views

urlpatterns = [
    path('register/',        views.RegisterView.as_view(),       name='register'),
    path('login/',           views.LoginView.as_view(),          name='login'),
    path('login/2fa/',       views.LoginTwoFactorView.as_view(), name='login-2fa'),
    path('2fa/',             views.TwoFactorView.as_view(),      name='two-factor'),
    path('2fa/backup-codes/', views.TwoFactorBackupCodesView.as_view(), name='two-factor-backup-codes'),
    path('logout/',          views.LogoutView.as_view(),         name='logout'),
    path('logout-all/',      views.LogoutAllView.as_view(),      name='logout-all'),
    path('me/',              views.MeView.as_view(),             name='me'),
    path('forgot-password/', views.ForgotPasswordView.as_view(), name='forgot-password'),
    path('reset-password/',  views.ResetPasswordView.as_view(),  name='reset-password'),
    path('token/refresh/',   TokenRefreshView.as_view(),         name='token-refresh'),
    path('change-password/', views.ChangePasswordView.as_view(), name='change-password'),
]