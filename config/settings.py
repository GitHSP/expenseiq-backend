# config/settings.py

import os
from pathlib import Path
from datetime import timedelta
from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Loads backend/.env locally. In production (PythonAnywhere/Render), set
# these as real environment variables in the host's dashboard instead —
# .env is gitignored and never deployed.
load_dotenv(BASE_DIR / '.env')

# SECURITY WARNING: keep the secret key used in production secret!
# Never hardcode this — set SECRET_KEY in backend/.env (local) or in your
# host's environment variables (production).
SECRET_KEY = os.environ['SECRET_KEY']

# SECURITY WARNING: don't run with debug turned on in production!
# Defaults to False — set DEBUG=True in backend/.env for local development.
DEBUG = os.environ.get('DEBUG', 'False') == 'True'

ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get(
        'ALLOWED_HOSTS',
        'localhost,127.0.0.1,sarathpeethu.pythonanywhere.com,.pythonanywhere.com'
    ).split(',') if h.strip()
]

# Used to build the password-reset link sent by email — point this at the
# real frontend URL in production (e.g. https://expenseiq-frontend-ten.vercel.app)
FRONTEND_URL = os.environ.get('FRONTEND_URL', 'http://localhost:3000')

# ── AI Assistant (Claude API) ─────────────────
# Server-side only — never sent to the frontend. Get a key at
# https://console.anthropic.com/settings/keys and set it in backend/.env.
# The chat feature is disabled (returns a clear error) until this is set.
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
ANTHROPIC_MODEL   = os.environ.get('ANTHROPIC_MODEL', 'claude-sonnet-4-5-20250929')

# ── Installed Apps ────────────────────────────
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third party
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',

    # Our apps
    'authentication',
    'expenses',
    'financial_planner',
    'assistant',
]

# ── Middleware ────────────────────────────────
MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',  # must be first
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# ── Database ──────────────────────────────────
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# ── Password Validation ───────────────────────
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ── Internationalisation ──────────────────────
LANGUAGE_CODE = 'en-us'
TIME_ZONE     = 'UTC'
USE_I18N      = True
USE_TZ        = True

# ── Static Files ──────────────────────────────
STATIC_URL = 'static/'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ── Custom User Model ─────────────────────────
AUTH_USER_MODEL = 'authentication.User'

# ── CORS Settings ─────────────────────────────
# Allows React (localhost:3000) to talk to Django (localhost:8000)
CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "https://expenseiq-frontend-ten.vercel.app",
]

# ── REST Framework ────────────────────────────
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    # Rate-limit unauthenticated requests — login/register/password-reset
    # are AllowAny, so without this they're open to brute-forcing.
    'DEFAULT_THROTTLE_CLASSES': (
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ),
    'DEFAULT_THROTTLE_RATES': {
        'anon': '20/minute',
        'user': '120/minute',
    },
}

# ── JWT Settings ──────────────────────────────
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME':  timedelta(days=1),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS':  True,
    'BLACKLIST_AFTER_ROTATION': True,
}

# ── Email Settings ────────────────────────────
# Development — prints emails to terminal
EMAIL_BACKEND   = 'django.core.mail.backends.console.EmailBackend'
DEFAULT_FROM_EMAIL = 'ExpenseIQ <noreply@expenseiq.com>'