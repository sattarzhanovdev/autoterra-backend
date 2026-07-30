import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-autoterra-change-me")
DEBUG = os.environ.get("DEBUG", "True").lower() in ("true", "1", "t")
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "*").split(",")

# Firebase Cloud Messaging — path to the service-account JSON from Firebase Console
FCM_SERVICE_ACCOUNT_FILE = os.environ.get(
    "FCM_SERVICE_ACCOUNT_FILE",
    str(BASE_DIR / "firebase-service-account.json"),
)

# Upload limits (20MB)
DATA_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "api",
]

# Keep startup stable if a local admin theme or environment patch adds an app twice.
INSTALLED_APPS = list(dict.fromkeys(INSTALLED_APPS))

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "api.middleware.ApiStatelessMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:8080",
    "http://127.0.0.1:8000",
    "http://localhost:49581",
    "http://127.0.0.1:49581",
    "http://89.111.132.221",
    "http://89.111.132.221:8000",
    "https://autoterra.ru",
    "http://localhost:38913",
    # Flutter web dev server
    "http://localhost:5000",
    "http://127.0.0.1:5000",
    "http://localhost:8081",
    "http://localhost:8082",
    "http://localhost:9090",
]
CORS_ALLOW_ALL_ORIGINS = DEBUG  # wildcard only when DEBUG=True (local dev)
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_METHODS = [
    "DELETE",
    "GET",
    "OPTIONS",
    "PATCH",
    "POST",
    "PUT",
]
from corsheaders.defaults import default_headers  # noqa: E402

CORS_ALLOW_HEADERS = [
    *default_headers,
    "x-integration-token",
]

ROOT_URLCONF = "autoterra_backend.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "autoterra_backend.wsgi.application"

# Поиск по товарам. Пока URL пуст, работает поиск средствами БД — отдельный
# сервис поднимать не требуется. Задайте адрес, чтобы включить Elasticsearch.
ELASTICSEARCH_URL = os.environ.get("ELASTICSEARCH_URL", "")
ELASTICSEARCH_INDEX = os.environ.get("ELASTICSEARCH_INDEX", "autoterra-products")
ELASTICSEARCH_TIMEOUT = float(os.environ.get("ELASTICSEARCH_TIMEOUT", "3"))

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "Asia/Bishkek"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = os.path.join(BASE_DIR, 'static')
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

HF_API_TOKEN = os.environ.get("HF_API_TOKEN", "")
HF_CHAT_MODEL = os.environ.get("HF_CHAT_MODEL", "openai/gpt-oss-120b")
HF_CHAT_URL = os.environ.get(
    "HF_CHAT_URL",
    "https://router.huggingface.co/v1/chat/completions",
)

# ── Email (SMTP) ──────────────────────────────────────────────────────────────
# Used to notify the manager by email whenever a new order is placed.
# Credentials live in .env; defaults fall back to the Django console backend so
# local dev without SMTP creds still works (emails are printed to the console).
EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "True").lower() in ("true", "1", "t")
EMAIL_USE_SSL = os.environ.get("EMAIL_USE_SSL", "False").lower() in ("true", "1", "t")
EMAIL_TIMEOUT = int(os.environ.get("EMAIL_TIMEOUT", "15"))

if EMAIL_HOST_USER and EMAIL_HOST_PASSWORD:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
else:
    # No credentials configured — don't fail, just print to the console.
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", EMAIL_HOST_USER or "no-reply@autoterra.ru")

# Where new-order notification emails are sent (comma-separated for multiple).
ORDER_NOTIFICATION_EMAILS = [
    addr.strip()
    for addr in os.environ.get("ORDER_NOTIFICATION_EMAILS", "").split(",")
    if addr.strip()
]

# Реферальная программа: подарок начисляется не за регистрацию, а только за
# реальную покупку приглашённого сервиса выше порога (п. 7 ТЗ).
REFERRAL_BONUS_THRESHOLD = int(os.environ.get("REFERRAL_BONUS_THRESHOLD", "30000"))
REFERRAL_BONUS_GIFT = os.environ.get("REFERRAL_BONUS_GIFT", "Сертификат на 5000 ₽")

# Базовый адрес для ссылки-приглашения: <база>/register?ref=<код>.
REFERRAL_INVITE_BASE_URL = os.environ.get(
    "REFERRAL_INVITE_BASE_URL", "https://autoterra.shop/register"
)

# Куда уходит письмо о регистрации нового клиента. Через запятую — несколько
# адресов. Пусто — используется ящик по умолчанию, зашитый в код.
REGISTRATION_NOTIFICATION_EMAILS = [
    addr.strip()
    for addr in os.environ.get(
        "REGISTRATION_NOTIFICATION_EMAILS", "zavtoterra@yandex.ru"
    ).split(",")
    if addr.strip()
]

# ── Payments: YooKassa (ЮKassa / YooMoney для бизнеса) ─────────────────────────
# shopId и секретный ключ берутся из личного кабинета ЮKassa.
# https://yookassa.ru/my  → Настройки → Магазин.
YOOKASSA_SHOP_ID = os.environ.get("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.environ.get("YOOKASSA_SECRET_KEY", "")
YOOKASSA_API_URL = os.environ.get("YOOKASSA_API_URL", "https://api.yookassa.ru/v3")
# Куда вернётся пользователь после оплаты (deep-link в приложение или веб-страница)
YOOKASSA_RETURN_URL = os.environ.get("YOOKASSA_RETURN_URL", "https://autoterra.ru/payment/return")
# Секрет для проверки входящих webhook-ов (необязателен; см. views).
YOOKASSA_WEBHOOK_SECRET = os.environ.get("YOOKASSA_WEBHOOK_SECRET", "")
