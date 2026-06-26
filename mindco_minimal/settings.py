from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', 'mindco-minimal-dev-secret-key')
DEBUG = os.getenv('DJANGO_DEBUG', '1') == '1'
ALLOWED_HOSTS = os.getenv('DJANGO_ALLOWED_HOSTS', '*').split(',')

SESSION_COOKIE_NAME = "sessionid_mindco_c"
CSRF_COOKIE_NAME = "csrftoken_mindco_c"

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'core.apps.CoreConfig',
    'service_management.apps.ServiceManagementConfig',
    'aca.apps.AcaConfig',
    'rcm.apps.RcmConfig',
    'pautas.apps.PautasConfig',
    'technical_locations.apps.TechnicalLocationsConfig',
    'evaluation_tables.apps.EvaluationTablesConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'mindco_minimal.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'core' / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'core.context_processors.navigation_context',
                'core.context_processors.empresa_usuario',
            ],
        },
    },
]

WSGI_APPLICATION = 'mindco_minimal.wsgi.application'

ALLOWED_HOSTS = [
    "127.0.0.1",
    "localhost",
    ".trycloudflare.com",
]

CSRF_TRUSTED_ORIGINS = [
    "https://*.trycloudflare.com",
]

DATABASES = { 
    'default': { 
        'ENGINE': 'django.db.backends.mysql', 
        'NAME': 'Mindcodbk', 
        'USER': 'root', 
        'PASSWORD': 
        'FelipeS@002', 
        'HOST': '127.0.0.1', 
        'PORT': '3306', 
        'OPTIONS': { 
            'charset': 'utf8mb4', 
        },
    } 
}

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = 'es-cl'
TIME_ZONE = 'America/Santiago'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static'] if (BASE_DIR / 'static').exists() else []
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'dashboard'
LOGOUT_REDIRECT_URL = 'login'