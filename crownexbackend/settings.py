"""
Django settings for crownexbackend project.
"""

import os
from datetime import timedelta
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')


# See https://docs.djangoproject.com/en/6.0/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
# Set a real SECRET_KEY in the environment for any deployed instance.
SECRET_KEY = os.environ.get(
    'SECRET_KEY', 'django-insecure-qd6c%1itpy)p51lifdd1i5ujav^h)eif6=wd3s16$l)itx&#q+'
)

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get('DEBUG', 'False').lower() == 'true'

ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1,10.0.2.2,0.0.0.0').split(',')
    if h.strip()
]

# Railway assigns a public domain at deploy time; trust it automatically.
_railway_domain = os.environ.get('RAILWAY_PUBLIC_DOMAIN')
if _railway_domain:
    ALLOWED_HOSTS.append(_railway_domain)

CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()
]
if _railway_domain:
    CSRF_TRUSTED_ORIGINS.append(f'https://{_railway_domain}')

# Railway terminates TLS at the edge and proxies to the app over HTTP.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True
SECURE_SSL_REDIRECT = not DEBUG


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'cloudinary_storage',
    'django.contrib.staticfiles',
    'cloudinary',
    # Third-party
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',
    # Local
    'accounts',
    'wallet',
    'vtu',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'crownexbackend.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'crownexbackend.wsgi.application'


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases
# Railway injects DATABASE_URL when a Postgres plugin is attached; falls
# back to local sqlite when it's absent (local dev).

DATABASES = {
    'default': dj_database_url.config(
        default=f'sqlite:///{BASE_DIR / "db.sqlite3"}',
        conn_max_age=600,
    )
}


# Custom user model
AUTH_USER_MODEL = 'accounts.User'


# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {'min_length': 8},
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

# User-uploaded media (profile pictures) — Railway's filesystem is ephemeral,
# so these go to Cloudinary when configured. Falls back to local disk when
# the env vars are absent (local dev, no Cloudinary account needed).
CLOUDINARY_STORAGE = {
    'CLOUD_NAME': os.environ.get('CLOUDINARY_CLOUD_NAME', ''),
    'API_KEY': os.environ.get('CLOUDINARY_API_KEY', ''),
    'API_SECRET': os.environ.get('CLOUDINARY_API_SECRET', ''),
}

STORAGES = {
    'default': {
        'BACKEND': (
            'cloudinary_storage.storage.MediaCloudinaryStorage'
            if CLOUDINARY_STORAGE['CLOUD_NAME']
            else 'django.core.files.storage.FileSystemStorage'
        ),
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}


# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# Django REST Framework
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_RENDERER_CLASSES': (
        'rest_framework.renderers.JSONRenderer',
    ),
    'EXCEPTION_HANDLER': 'rest_framework.views.exception_handler',
}


# Simple JWT
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=60),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
    'AUTH_TOKEN_CLASSES': ('rest_framework_simplejwt.tokens.AccessToken',),
}


# CORS — wide open in dev; explicit allowlist required in production
# (mobile clients aren't subject to CORS, this mainly matters for Flutter web).
CORS_ALLOW_ALL_ORIGINS = DEBUG
CORS_ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get('CORS_ALLOWED_ORIGINS', '').split(',') if o.strip()
]
CORS_ALLOW_CREDENTIALS = True


# Email — defaults to console (OTP prints in runserver output).
# To send through SendGrid instead, set in .env (see .env.example):
#   EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
#   SENDGRID_API_KEY=SG.xxxxxxxx
# No code changes needed to switch.
EMAIL_BACKEND = os.environ.get('EMAIL_BACKEND', 'django.core.mail.backends.console.EmailBackend')
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'smtp.sendgrid.net')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', 587))
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', 'true').lower() == 'true'
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', 'apikey')  # SendGrid SMTP always uses literal 'apikey'
EMAIL_HOST_PASSWORD = os.environ.get('SENDGRID_API_KEY', '')
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'CrownEx <noreply@crownex.app>')


# OTP settings
OTP_LENGTH = 4
OTP_EXPIRY_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_COOLDOWN_SECONDS = 60


# Flutterwave — NGN wallet deposits/withdrawals (wallet app).
# Secret key and webhook hash are server-only; never expose them to the app.
APP_NAME = os.environ.get('APP_NAME', 'CrownEx')
FLW_BASE_URL = os.environ.get('FLW_BASE_URL', 'https://api.flutterwave.com/v3')
FLW_SECRET_KEY = os.environ.get('FLW_SECRET_KEY', '')
FLW_PUBLIC_KEY = os.environ.get('FLW_PUBLIC_KEY', '')
FLW_WEBHOOK_HASH = os.environ.get('FLW_WEBHOOK_HASH', '')


# PluginNG — VTU purchases: airtime, data (vtu app).
# Bearer token is a personal access token from your PluginNG dashboard —
# never expose it to the app. Webhook secret is a random string you set both
# here and as a path segment in PluginNG's webhook URL config, since PluginNG
# doesn't sign its webhook payloads.
PLUGINNG_BASE_URL = os.environ.get('PLUGINNG_BASE_URL', 'https://pluginng.com/api')
PLUGINNG_TOKEN = os.environ.get('PLUGINNG_TOKEN', '')
PLUGINNG_WEBHOOK_SECRET = os.environ.get('PLUGINNG_WEBHOOK_SECRET', '')
