import hashlib
import json
import logging
import secrets
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import get_password_validators, validate_password
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import F, Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST

from .models import AuthToken, PasswordResetRateLimit, PasswordResetRequest

logger = logging.getLogger(__name__)
REQUEST_MESSAGE = "Если аккаунт с такой почтой существует, мы отправим ссылку для восстановления пароля."
INVALID_LINK = "Ссылка недействительна или срок её действия истёк. Запросите новую."


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def payload(request):
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def allowed(scope, value, limit):
    # Shared database counters work across workers, including on SQLite.
    window = int(timezone.now().timestamp()) // 3600
    key = digest(f"{scope}:{window}:{value}")
    row, _ = PasswordResetRateLimit.objects.get_or_create(key=key)
    return bool(PasswordResetRateLimit.objects.filter(pk=row.pk, count__lt=limit).update(count=F("count") + 1))


def reset_url_configured():
    url = urlsplit(settings.PASSWORD_RESET_URL)
    return bool(url.scheme == "https" and url.netloc and not url.query and not url.fragment and not url.username)


@csrf_exempt
@require_POST
def request_reset(request):
    data = payload(request)
    if isinstance(data, dict) and "email" not in data and any(key in data for key in ("phone", "username")):
        # Keep existing mobile clients using phone/INN/email-code compatible.
        scope, limit = ("confirm-ip", 60) if data.get("code") else ("request-ip", 20)
        if not allowed(scope, request.META.get("REMOTE_ADDR", ""), limit):
            return JsonResponse({"detail": "Слишком много запросов. Попробуйте через час."}, status=429)
        from .views import password_reset as legacy_reset
        return legacy_reset(request)
    email = data.get("email") if data else None
    if not isinstance(email, str):
        return JsonResponse({"detail": "Укажите email."}, status=400)
    email = email.strip().lower()
    try:
        validate_email(email)
        if len(email) > 254:
            raise ValidationError("email")
    except ValidationError:
        return JsonResponse({"detail": "Укажите корректный email."}, status=400)
    if not reset_url_configured():
        return JsonResponse({"detail": "Восстановление пароля пока не настроено."}, status=503)
    # REMOTE_ADDR must be set by a trusted proxy; never trust client X-Forwarded-For.
    if not allowed("request-ip", request.META.get("REMOTE_ADDR", ""), 20):
        response = JsonResponse({"detail": "Слишком много запросов. Попробуйте через час."}, status=429)
        response["Retry-After"] = "3600"
        return response
    if allowed("request-email", email, 3):
        users = list(User.objects.filter(email__iexact=email, is_active=True)[:2])
        # Ambiguous emails require manual correction, never choose an arbitrary account.
        if len(users) == 1 and users[0].has_usable_password():
            user = users[0]
            PasswordResetRequest.objects.create(user=user, email=user.email, credential_hash=digest(user.password))
    return JsonResponse({"detail": REQUEST_MESSAGE}, status=202)


@sensitive_post_parameters("new_password", "token")
@csrf_exempt
@require_POST
def confirm_reset(request):
    if not allowed("confirm-ip", request.META.get("REMOTE_ADDR", ""), 60):
        return JsonResponse({"detail": "Слишком много попыток. Попробуйте через час."}, status=429)
    data = payload(request)
    if not data or not all(isinstance(data.get(key), str) for key in ("token", "new_password")):
        return JsonResponse({"detail": "Передайте token и new_password."}, status=400)
    token, password = data["token"], data["new_password"]
    if len(token) != 64:
        return JsonResponse({"detail": INVALID_LINK}, status=400)
    with transaction.atomic():
        job = PasswordResetRequest.objects.select_for_update().filter(
            token_hash=digest(token), status="sent", consumed_at__isnull=True, expires_at__gt=timezone.now()
        ).first()
        if job is None:
            return JsonResponse({"detail": INVALID_LINK}, status=400)
        user = User.objects.select_for_update().get(pk=job.user_id)
        if not user.is_active or user.email != job.email or digest(user.password) != job.credential_hash:
            return JsonResponse({"detail": INVALID_LINK}, status=400)
        try:
            if len(password) > 128:
                raise ValidationError("Пароль должен содержать не более 128 символов.")
            validate_password(password, user, get_password_validators(settings.PASSWORD_RECOVERY_VALIDATORS))
            if user.check_password(password):
                raise ValidationError("Новый пароль должен отличаться от текущего.")
        except ValidationError as exc:
            return JsonResponse({"detail": "Проверьте новый пароль.", "errors": exc.messages}, status=400)
        user.set_password(password)
        user.save(update_fields=["password"])
        PasswordResetRequest.objects.filter(user=user, consumed_at__isnull=True).update(
            consumed_at=timezone.now(), token_hash="", status="consumed"
        )
        AuthToken.objects.filter(user=user).delete()
    return JsonResponse({"detail": "Пароль изменён. Войдите с новым паролем."})


def deliver_reset_emails(limit=100):
    if not reset_url_configured():
        raise ValueError("Set PASSWORD_RESET_URL to the HTTPS frontend reset page without query or fragment.")
    now = timezone.now()
    # Match the existing registration worker: lease exceeds the SMTP timeout.
    lease_seconds = max(settings.EMAIL_TIMEOUT * 4, 300)
    eligible = Q(status__in=["pending", "failed"]) | Q(
        status="sending", claimed_at__lt=now - timedelta(seconds=lease_seconds)
    )
    ids = list(PasswordResetRequest.objects.filter(eligible, attempts__lt=3, consumed_at__isnull=True,
               created_at__gt=now - timedelta(hours=1)).order_by("pk").values_list("pk", flat=True)[:limit])
    sent = 0
    for job_id in ids:
        claimed = PasswordResetRequest.objects.filter(eligible, pk=job_id, attempts__lt=3, consumed_at__isnull=True).update(
            status="sending", claimed_at=timezone.now(), attempts=F("attempts") + 1
        )
        if not claimed:
            continue
        job = PasswordResetRequest.objects.select_related("user").get(pk=job_id)
        if not job.user.is_active or job.user.email != job.email or digest(job.user.password) != job.credential_hash:
            PasswordResetRequest.objects.filter(pk=job_id).update(status="cancelled", token_hash="")
            continue
        token = secrets.token_hex(32)
        expires_at = timezone.now() + timedelta(seconds=settings.PASSWORD_RESET_TIMEOUT)
        PasswordResetRequest.objects.filter(pk=job_id).update(token_hash=digest(token), expires_at=expires_at)
        url = settings.PASSWORD_RESET_URL + "?" + urlencode({"token": token})
        try:
            if settings.EMAIL_BACKEND in (
                "django.core.mail.backends.console.EmailBackend",
                "django.core.mail.backends.dummy.EmailBackend",
                "django.core.mail.backends.filebased.EmailBackend",
            ):
                raise RuntimeError("SMTP delivery is not configured")
            delivered = send_mail(
                "Восстановление пароля АвтоТерра",
                f"Для установки нового пароля откройте ссылку:\n\n{url}\n\n"
                f"Ссылка действует {settings.PASSWORD_RESET_TIMEOUT // 60} минут и используется один раз.\n"
                "Если вы не запрашивали восстановление, просто проигнорируйте письмо.",
                settings.DEFAULT_FROM_EMAIL, [job.email], fail_silently=False,
            )
            if delivered != 1:
                raise RuntimeError("Mail backend did not accept the message")
        except Exception:
            # Do not log SMTP exceptions: providers may include credentials or email contents.
            logger.error("Password reset email delivery failed for job %s", job_id)
            PasswordResetRequest.objects.filter(pk=job_id, status="sending").update(status="failed", token_hash="")
        else:
            sent += PasswordResetRequest.objects.filter(pk=job_id, status="sending").update(status="sent")
    return sent
