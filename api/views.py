import json
import logging
import secrets
import ssl
import urllib.error
import urllib.request
import csv
import io
from datetime import date, datetime, time as dt_time
from decimal import Decimal, InvalidOperation
from hashlib import sha256

from django.contrib.auth import authenticate, login as django_login
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.models import User
from django.conf import settings
from django.db import models, transaction
from django.db.utils import IntegrityError
from django.db.models import Q, Sum, F
from django.http import JsonResponse, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.html import escape
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .models import (
    Attachment,
    AuthToken,
    AuditLog,
    ClientProfile,
    ColorRequest,
    CourierTask,
    Distributor,
    ExpertTicket,
    KnowledgeCard,
    BonusTransaction,
    LearningMaterial,
    Notification,
    Order,
    OrderAdjustment,
    OrderItem,
    Payment,
    Product,
    Profile,
    Purchase,
    PurchaseItem,
    Region,
    Referral,
    RecipeMaterial,
    Store,
    IntegrationToken,
    SyncLog,
    ManagerTask,
    ContactHistory,
    grown_partner_status,
    normalize_product_images,
)
from .pagination import paginate, paginated_response
from .serializers import RegistrationSerializer, PurchaseSerializer, coerce_decimal
from .services.bonuses import balance as bonus_balance
from .services.exports import EXPORTERS as EXPORT_FORMATS, ExportUnavailable, export_clients
from .services.inn import is_valid_inn
from .services.pricing import price_details, price_for_client
from .services.tiers import sync_client_tier

try:
    import certifi
except ImportError:
    certifi = None


logger = logging.getLogger(__name__)


def _notify(user, title, body, n_type="info", link=""):
    """Create a Notification for ``user`` and best-effort send a push.

    Never raises — notification/push failures must not break the request that
    triggered them (order/purchase/delivery/color-request creation).
    """
    if user is None:
        return None
    try:
        notification = Notification.objects.create(
            user=user,
            title=title,
            body=body,
            type=n_type,
            related_link=link,
        )
    except Exception:
        logger.exception("Failed to create notification for user %s", getattr(user, "id", None))
        return None
    try:
        from api.services.push_notifications import PushNotificationService
        PushNotificationService().send(notification)
    except Exception:
        logger.exception("Failed to send push for notification %s", notification.pk)
    return notification


def _send_order_email(order):
    """Best-effort email to the manager mailbox on a new order.

    Never raises — email failures must not break order creation. Delivery is
    controlled by ORDER_NOTIFICATION_EMAILS in settings/.env; if empty, nothing
    is sent.
    """
    recipients = getattr(settings, "ORDER_NOTIFICATION_EMAILS", None)
    if not recipients:
        return
    try:
        from django.core.mail import EmailMultiAlternatives

        client = order.client
        store_name = order.store.name if order.store else "—"
        delivery = dict(Order.DELIVERY_CHOICES).get(order.delivery_method, order.delivery_method)
        order_no = f"ORD-{order.id:05d}"
        items = list(order.items.all())

        # ── Plain-text fallback ────────────────────────────────────────────────
        lines = [
            f"Новый заказ {order_no}",
            "",
            f"Клиент: {client.company_name}",
            f"ИНН: {client.inn}",
            f"Дистрибьютор: {order.distributor.name}",
            f"Способ получения: {delivery}",
            f"Магазин/точка: {store_name}",
            f"Дата: {order.created_at.strftime('%d.%m.%Y %H:%M')}",
        ]
        if order.comment:
            lines.append(f"Комментарий: {order.comment}")
        lines.append("")
        lines.append("Позиции:")
        for item in items:
            lines.append(
                f"  • {item.name} ({item.sku}) — {item.quantity} шт. x {item.price} = {item.total}"
            )
        lines.append("")
        lines.append(f"Итого: {order.total_amount} ₽")
        text_body = "\n".join(lines)

        # ── HTML version ───────────────────────────────────────────────────────
        rows = "".join(
            f"""
            <tr>
              <td style="padding:12px 16px;border-bottom:1px solid #eee;font-size:16px;color:#222;">
                <strong>{item.name}</strong><br>
                <span style="color:#888;font-size:14px;">Артикул: {item.sku}</span>
              </td>
              <td style="padding:12px 16px;border-bottom:1px solid #eee;font-size:16px;color:#222;text-align:center;">{item.quantity}&nbsp;шт.</td>
              <td style="padding:12px 16px;border-bottom:1px solid #eee;font-size:16px;color:#222;text-align:right;white-space:nowrap;">{item.price} ₽</td>
              <td style="padding:12px 16px;border-bottom:1px solid #eee;font-size:16px;color:#222;text-align:right;white-space:nowrap;"><strong>{item.total} ₽</strong></td>
            </tr>"""
            for item in items
        )

        info_row = lambda label, value: f"""
            <tr>
              <td style="padding:6px 0;font-size:16px;color:#888;width:190px;">{label}</td>
              <td style="padding:6px 0;font-size:16px;color:#222;"><strong>{value}</strong></td>
            </tr>"""

        comment_row = info_row("Комментарий", order.comment) if order.comment else ""

        html_body = f"""\
<!DOCTYPE html>
<html lang="ru">
<body style="margin:0;padding:0;background:#f4f5f7;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;padding:24px 0;">
    <tr><td align="center">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 4px 16px rgba(240,29,44,0.12);border:1px solid #f0e0e1;">
        <tr>
          <td style="background:#f01d2c;padding:30px 32px;border-bottom:4px solid #111111;">
            <div style="font-size:14px;color:#ffd9dc;letter-spacing:2px;text-transform:uppercase;font-weight:700;">AutoTerra</div>
            <div style="font-size:27px;color:#ffffff;font-weight:800;margin-top:8px;">Новый заказ {order_no}</div>
          </td>
        </tr>
        <tr>
          <td style="padding:28px 32px 8px 32px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
              {info_row("Клиент", client.company_name)}
              {info_row("ИНН", client.inn)}
              {info_row("Дистрибьютор", order.distributor.name)}
              {info_row("Способ получения", delivery)}
              {info_row("Магазин / точка", store_name)}
              {info_row("Дата", order.created_at.strftime("%d.%m.%Y %H:%M"))}
              {comment_row}
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:16px 32px 0 32px;">
            <div style="font-size:18px;color:#f01d2c;font-weight:800;margin-bottom:10px;">Позиции заказа</div>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #eee;border-radius:8px;overflow:hidden;">
              <tr style="background:#111111;">
                <td style="padding:11px 16px;font-size:13px;color:#ffffff;text-transform:uppercase;letter-spacing:0.5px;">Товар</td>
                <td style="padding:11px 16px;font-size:13px;color:#ffffff;text-transform:uppercase;letter-spacing:0.5px;text-align:center;">Кол-во</td>
                <td style="padding:11px 16px;font-size:13px;color:#ffffff;text-transform:uppercase;letter-spacing:0.5px;text-align:right;">Цена</td>
                <td style="padding:11px 16px;font-size:13px;color:#ffffff;text-transform:uppercase;letter-spacing:0.5px;text-align:right;">Сумма</td>
              </tr>
              {rows}
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:20px 32px 28px 32px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#fff2f3;border:1px solid #ffd9dc;border-radius:10px;">
              <tr>
                <td style="padding:16px 20px;font-size:20px;color:#111111;font-weight:700;">Итого:</td>
                <td style="padding:16px 20px;font-size:26px;color:#f01d2c;font-weight:800;text-align:right;">{order.total_amount} ₽</td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="background:#111111;padding:18px 32px;text-align:center;font-size:13px;color:#bbbbbb;">
            Это письмо сформировано автоматически платформой <span style="color:#f01d2c;font-weight:700;">AutoTerra</span>.
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

        msg = EmailMultiAlternatives(
            subject=f"Новый заказ {order_no} — {client.company_name}",
            body=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=list(recipients),
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send(fail_silently=False)
    except Exception:
        logger.exception("Failed to send order email for order %s", getattr(order, "id", None))


def _link_referral(client, code):
    """Связывает нового клиента с пригласившим по реферальному коду.

    Раньше запись о реферале мог создать только сам пригласивший, вручную вписав
    ИНН будущего клиента заранее. По ТЗ (п. 7) наоборот: клиент раздаёт ссылку
    или код, а система связывает пришедшего по нему при регистрации.

    Ничего не бросает: неверный код не должен ломать регистрацию.
    """
    code = (code or "").strip().upper()
    if not code:
        return None

    inviter = ClientProfile.objects.filter(referral_code=code).first()
    if inviter is None:
        logger.info("Регистрация с неизвестным реферальным кодом %s", code)
        return None
    if inviter.pk == client.pk:
        return None  # сам себя пригласить нельзя

    referral, created = Referral.objects.get_or_create(
        inviter=inviter,
        invitee_inn=client.inn,
        defaults={
            "invitee_name": client.company_name,
            "region": client.region.name if client.region_id else "",
            # Код — уже доказательство: клиент сам перешёл по этой ссылке.
            "confirmation": "auto",
        },
    )
    if not created and not referral.invitee_name:
        referral.invitee_name = client.company_name
        referral.save(update_fields=["invitee_name"])
    # Заявка была подана вручную заранее, а клиент пришёл по коду того же
    # человека — код подтверждает её задним числом.
    if referral.confirmation == "pending":
        referral.confirmation = "auto"
        referral.confirmed_at = timezone.now()
        referral.save(update_fields=["confirmation", "confirmed_at"])

    # Регистрация по коду — доказательство сильнее ручной заявки: клиент пришёл
    # именно по этой ссылке. Чужие заявки на тот же ИНН снимаем, иначе за одного
    # клиента подарок ушёл бы двоим. Трогаем только те, по которым ещё ничего не
    # решено, — согласованный подарок не отбираем.
    stale = (
        Referral.objects.filter(invitee_inn=client.inn)
        .exclude(pk=referral.pk)
        .filter(gift_status="none")
    )
    removed = stale.count()
    if removed:
        logger.info(
            "Сняты заявки на ИНН %s (%s шт.): клиент пришёл по коду %s",
            client.inn,
            removed,
            code,
        )
        stale.delete()

    # Регистрация — это ещё не покупка: подарок появится только после
    # подтверждённого заказа выше порога, это делает sync_from_invitee.
    referral.sync_from_invitee()
    return referral


def _send_registration_email(client):
    """Письмо о новой регистрации. Не бросает исключений.

    Вызывается ПОСЛЕ коммита транзакции: SMTP держит соединение до
    EMAIL_TIMEOUT секунд, и письмо о клиенте, чья транзакция откатилась,
    никому не нужно.
    """
    recipients = getattr(settings, "REGISTRATION_NOTIFICATION_EMAILS", None) or [
        "zavtoterra@yandex.ru"
    ]

    try:
        from django.core.mail import EmailMultiAlternatives

        status_display = (
            client.get_status_display()
            if hasattr(client, "get_status_display")
            else client.status
        )
        region_name = client.region.name if client.region_id else "—"

        # Plain-text fallback
        lines = [
            f"Новая регистрация: {client.company_name}",
            "",
            f"ИНН: {client.inn}",
            f"Регион: {region_name}",
            f"Город: {client.city}",
            f"Телефон: {client.phone}",
            f"Контактное лицо: {client.contact_name}",
            f"Источник: {client.get_registration_source_display()}",
            f"Статус: {status_display}",
        ]
        text_body = "\n".join(lines)

        # Данные вводит сам клиент, поэтому в HTML они идут только
        # экранированными: иначе кавычка в названии компании ломает вёрстку,
        # а <img onerror=...> превращает уведомление в вектор атаки.
        def info_row(label, value):
            return f"""
            <tr>
              <td style="padding:6px 0;font-size:16px;color:#888;width:190px;">{escape(label)}</td>
              <td style="padding:6px 0;font-size:16px;color:#222;"><strong>{escape(value)}</strong></td>
            </tr>"""

        html_body = f"""\
<!DOCTYPE html>
<html lang="ru">
<body style="margin:0;padding:0;background:#f4f5f7;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;padding:24px 0;">
    <tr><td align="center">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 4px 16px rgba(240,29,44,0.12);border:1px solid #f0e0e1;">
        <tr>
          <td style="background:#f01d2c;padding:30px 32px;border-bottom:4px solid #111111;">
            <div style="font-size:14px;color:#ffd9dc;letter-spacing:2px;text-transform:uppercase;font-weight:700;">AutoTerra</div>
            <div style="font-size:27px;color:#ffffff;font-weight:800;margin-top:8px;">Новая регистрация</div>
          </td>
        </tr>
        <tr>
          <td style="padding:28px 32px 28px 32px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
              {info_row("Компания", client.company_name)}
              {info_row("ИНН", client.inn)}
              {info_row("Регион", region_name)}
              {info_row("Город", client.city)}
              {info_row("Телефон", client.phone)}
              {info_row("Контактное лицо", client.contact_name)}
              {info_row("Статус", status_display)}
            </table>
          </td>
        </tr>
        <tr>
          <td style="background:#111111;padding:18px 32px;text-align:center;font-size:13px;color:#bbbbbb;">
            Это письмо сформировано автоматически платформой <span style="color:#f01d2c;font-weight:700;">AutoTerra</span>.
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

        msg = EmailMultiAlternatives(
            subject=f"Новая регистрация: {client.company_name} ({client.inn})",
            body=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=recipients,
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send(fail_silently=False)
    except Exception:
        logger.exception("Failed to send registration email for client %s", getattr(client, "id", None))


def _send_order_status_email(order, old_status, new_status, recipients=None):
    """Best-effort email on order status change. Never raises.

    ``recipients`` overrides the default operator mailbox — used to email the
    client (order.client.user.email) directly.
    """
    if recipients is None:
        recipients = getattr(settings, "ORDER_NOTIFICATION_EMAILS", None)
    recipients = [r for r in (recipients or []) if r]
    if not recipients or old_status == new_status:
        return
    try:
        from django.core.mail import EmailMultiAlternatives

        status_labels = dict(Order.STATUS_CHOICES)
        # Header colour per status: accepted/fulfilled → red brand, rejected → black
        accent = "#111111" if new_status == "rejected" else "#f01d2c"
        old_label = status_labels.get(old_status, old_status)
        new_label = status_labels.get(new_status, new_status)
        order_no = f"ORD-{order.id:05d}"
        client = order.client

        text_lines = [
            f"Заказ {order_no} — статус изменён",
            "",
            f"Клиент: {client.company_name}",
            f"Было: {old_label}",
            f"Стало: {new_label}",
        ]
        if new_status == "rejected" and order.rejection_reason:
            text_lines.append(f"Причина: {order.rejection_reason}")
        text_lines.append(f"Сумма заказа: {order.total_amount} ₽")
        text_body = "\n".join(text_lines)

        reason_block = ""
        if new_status == "rejected" and order.rejection_reason:
            reason_block = f"""
              <tr>
                <td style="padding:6px 0;font-size:16px;color:#888;width:190px;">Причина</td>
                <td style="padding:6px 0;font-size:16px;color:#222;"><strong>{order.rejection_reason}</strong></td>
              </tr>"""

        html_body = f"""\
<!DOCTYPE html>
<html lang="ru">
<body style="margin:0;padding:0;background:#f4f5f7;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;padding:24px 0;">
    <tr><td align="center">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 4px 16px rgba(240,29,44,0.12);border:1px solid #f0e0e1;">
        <tr>
          <td style="background:{accent};padding:30px 32px;border-bottom:4px solid #111111;">
            <div style="font-size:14px;color:#ffd9dc;letter-spacing:2px;text-transform:uppercase;font-weight:700;">AutoTerra</div>
            <div style="font-size:27px;color:#ffffff;font-weight:800;margin-top:8px;">Заказ {order_no}</div>
          </td>
        </tr>
        <tr>
          <td style="padding:28px 32px 12px 32px;">
            <div style="font-size:20px;color:#111111;font-weight:700;margin-bottom:18px;">
              Статус: <span style="color:#888;">{old_label}</span> &nbsp;→&nbsp; <span style="color:{accent};">{new_label}</span>
            </div>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="padding:6px 0;font-size:16px;color:#888;width:190px;">Клиент</td>
                <td style="padding:6px 0;font-size:16px;color:#222;"><strong>{client.company_name}</strong></td>
              </tr>
              <tr>
                <td style="padding:6px 0;font-size:16px;color:#888;width:190px;">ИНН</td>
                <td style="padding:6px 0;font-size:16px;color:#222;"><strong>{client.inn}</strong></td>
              </tr>
              <tr>
                <td style="padding:6px 0;font-size:16px;color:#888;width:190px;">Сумма заказа</td>
                <td style="padding:6px 0;font-size:16px;color:#222;"><strong>{order.total_amount} ₽</strong></td>
              </tr>{reason_block}
            </table>
          </td>
        </tr>
        <tr>
          <td style="background:#111111;padding:18px 32px;text-align:center;font-size:13px;color:#bbbbbb;">
            Это письмо сформировано автоматически платформой <span style="color:#f01d2c;font-weight:700;">AutoTerra</span>.
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

        msg = EmailMultiAlternatives(
            subject=f"Заказ {order_no}: {new_label} — {client.company_name}",
            body=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=list(recipients),
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send(fail_silently=False)
    except Exception:
        logger.exception("Failed to send status email for order %s", getattr(order, "id", None))


MAX_UPLOAD_SIZE = 10 * 1024 * 1024
ALLOWED_UPLOAD_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".pdf", ".mp4", ".mov"}
ALLOWED_UPLOAD_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/pdf",
    "video/mp4",
    "video/quicktime",
}


def _json(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return {}


def _limit(request, qs, default_limit=50):
    """Ограничивает размер вложенного списка внутри составного ответа.

    Применяется там, где в одном ответе отдаётся сразу несколько списков и
    полноценная постраничная навигация невозможна. Для обычных списочных
    эндпоинтов используйте ``paginated_response`` из ``api.pagination``.
    """
    try:
        limit = int(request.GET.get("limit", default_limit))
        offset = int(request.GET.get("offset", 0))
    except ValueError:
        limit = default_limit
        offset = 0
    limit = min(limit, 100)
    return qs[offset:offset + limit]


def _normalize_phone(phone):
    normalized = "".join(ch for ch in (phone or "").strip() if ch.isdigit() or ch == "+")
    if normalized.startswith("8"):
        normalized = f"+7{normalized[1:]}"
    return normalized


def _normalize_inn(inn):
    return "".join(ch for ch in (inn or "").strip() if ch.isdigit())


def _normalize_category(category):
    value = (category or "b").strip().lower()
    return value if value in {"a", "b", "c"} else "b"


def _find_region(value):
    region_value = (value or "").strip()
    if not region_value:
        return None
    return (
        Region.objects.select_related("distributor", "manager")
        .filter(is_active=True)
        .filter(Q(code__iexact=region_value) | Q(name__iexact=region_value))
        .first()
    )

def _dt(value):
    if not value or str(value).lower() in ["null", "none", ""]:
        return None
    try:
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))

        if timezone.is_naive(parsed):
            return timezone.make_aware(parsed)
        return parsed
    except (ValueError, TypeError):
        return None


def _time(value):
    """Парсит время «до скольки» из «HH:MM», «HH:MM:SS» или ISO-даты."""
    if not value or str(value).lower() in ["null", "none", ""]:
        return None
    if isinstance(value, dt_time):
        return value
    raw = str(value).strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    parsed = _dt(raw)
    return timezone.localtime(parsed).time() if parsed else None


def _date(value):
    if not value or str(value).lower() in ["null", "none", ""]:
        return None
    try:
        if isinstance(value, date):
            return value
        dt_val = _dt(value)
        return dt_val.date() if dt_val else None
    except (ValueError, TypeError):
        return None


def _bool(value):
    if value is None:
        return False
    return str(value).lower() in {"true", "1", "yes", "y", "on"}


def _current_user(request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth.replace("Bearer ", "", 1).strip()
    obj = AuthToken.objects.filter(key=token).select_related("user").first()
    return obj.user if obj else None


def _require_client(request):
    user = _current_user(request)
    if user is None:
        return None, JsonResponse({"detail": "Unauthorized"}, status=401)
    
    role = getattr(user, "profile", None).role if hasattr(user, "profile") else "client"
    if role != "client":
        return None, JsonResponse({"detail": "Нет доступа клиента"}, status=403)
        
    try:
        return user.client_profile, None
    except ClientProfile.DoesNotExist:
        return None, JsonResponse({"detail": "Профиль клиента не создан в admin"}, status=403)


def _require_distributor_scope(request):
    user = _current_user(request)
    if user is None:
        return None, False, JsonResponse({"detail": "Unauthorized"}, status=401)
    if user.is_staff or user.is_superuser:
        return None, True, None
        
    role = getattr(user, "profile", None).role if hasattr(user, "profile") else "unknown"
    if role != "distributor":
        return None, False, JsonResponse({"detail": "Нет доступа дистрибьютора"}, status=403)

    distributor = getattr(user, "distributor_profile", None)
    if distributor is None:
        return None, False, JsonResponse({"detail": "Профиль дистрибьютора не создан в admin"}, status=403)
    return distributor, False, None


def _is_courier_user(user):
    if not user:
        return False
    if user.is_staff or user.is_superuser:
        return True
    return getattr(user, "profile", None) and user.profile.role == "courier"


def _require_courier_scope(request):
    user = _current_user(request)
    if user is None:
        return None, False, JsonResponse({"detail": "Unauthorized"}, status=401)
    if not _is_courier_user(user):
        return None, False, JsonResponse({"detail": "Нет доступа курьера"}, status=403)
    return user, bool(user.is_staff or user.is_superuser), None


def _is_expert_user(user):
    if not user:
        return False
    if user.is_staff or user.is_superuser:
        return True
    return getattr(user, "profile", None) and user.profile.role == "ai_expert"


def _require_expert_scope(request):
    user = _current_user(request)
    if user is None:
        return None, False, JsonResponse({"detail": "Unauthorized"}, status=401)
    if not _is_expert_user(user):
        return None, False, JsonResponse({"detail": "Нет доступа эксперта"}, status=403)
    return user, bool(user.is_staff or user.is_superuser), None


def _scope_courier_tasks(user, is_admin):
    qs = CourierTask.objects.select_related("client", "assigned_courier", "order", "color_request")
    if is_admin:
        return qs
    return qs.filter(Q(assigned_courier=user) | Q(courier_id=str(user.id)) | Q(courier_id=user.username))


def _append_task_history(task, status, user=None, comment=""):
    item = {
        "status": status,
        "at": timezone.now().isoformat(),
        "by": str(user.id) if user else None,
        "comment": comment,
    }
    task.status_history = [*(task.status_history or []), item]


def _scope_clients(distributor, is_admin):
    qs = ClientProfile.objects.select_related("distributor", "manager", "user", "region")
    return qs if is_admin else qs.filter(region__distributor=distributor)


def _scope_purchases(distributor, is_admin):
    qs = Purchase.objects.select_related("client", "distributor").prefetch_related("items")
    return qs if is_admin else qs.filter(distributor=distributor)


def _scope_orders(distributor, is_admin):
    qs = Order.objects.select_related("client", "store", "distributor").prefetch_related("items")
    return qs if is_admin else qs.filter(distributor=distributor)


def _scope_courier_tasks(distributor, is_admin):
    qs = CourierTask.objects.select_related("client", "courier", "order", "color_request")
    return qs if is_admin else qs.filter(client__distributor=distributor)


def _scope_products(distributor, is_admin):
    qs = Product.objects.select_related("distributor")
    return qs if is_admin else qs.filter(distributor=distributor)


def _money_value(value):
    # Normalise common input quirks (spaces as thousands separators, comma decimals)
    # then bound the result to the DecimalField(max_digits=12, decimal_places=2) used by
    # Product.price / PurchaseItem.price. Falls back to 0 so an invalid or oversized
    # value can never be stored and later crash the SQLite decimal converter on read.
    cleaned = str(value or "0").replace(" ", "").replace(",", ".")
    coerced = coerce_decimal(cleaned, max_digits=12, decimal_places=2)
    return coerced if coerced is not None and coerced >= 0 else Decimal("0")


def _parse_items(value):
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return decoded if isinstance(decoded, list) else []


def _uploaded_file_hash(file_obj):
    if not file_obj:
        return ""
    digest = sha256()
    for chunk in file_obj.chunks():
        digest.update(chunk)
    file_obj.seek(0)
    return digest.hexdigest()


def _file_extension(name):
    lowered = (name or "").lower()
    if "." not in lowered:
        return ""
    return lowered[lowered.rfind(".") :]


def _file_type(file_obj):
    content_type = (getattr(file_obj, "content_type", "") or "").lower()
    extension = _file_extension(getattr(file_obj, "name", ""))
    if content_type.startswith("image/") or extension in {".jpg", ".jpeg", ".png", ".webp"}:
        return "image"
    if content_type == "application/pdf" or extension == ".pdf":
        return "pdf"
    if content_type.startswith("video/") or extension in {".mp4", ".mov"}:
        return "video"
    return "document"


def _upload_error(file_obj):
    extension = _file_extension(getattr(file_obj, "name", ""))
    content_type = (getattr(file_obj, "content_type", "") or "").lower()
    if extension not in ALLOWED_UPLOAD_EXTENSIONS:
        return JsonResponse({"detail": "Недопустимый тип файла", "code": "invalid_file_type"}, status=400)
    # Mobile clients often send a missing or generic content type (octet-stream) even
    # for valid JPG/PDF uploads. The extension is already validated above, so only
    # reject when the client sent a specific, disallowed content type.
    if content_type and content_type not in ALLOWED_UPLOAD_CONTENT_TYPES and content_type != "application/octet-stream":
        return JsonResponse({"detail": "Недопустимый тип файла", "code": "invalid_file_type"}, status=400)
    if getattr(file_obj, "size", 0) > MAX_UPLOAD_SIZE:
        return JsonResponse({"detail": "Файл больше 10 МБ", "code": "file_too_large"}, status=400)
    return None


def _attachment_files(request):
    files = []
    seen = set()
    for field in ("attachments", "document", "file", "photo", "proof", "files"):
        for file_obj in request.FILES.getlist(field):
            marker = id(file_obj)
            if marker not in seen:
                files.append(file_obj)
                seen.add(marker)
    return files


def _create_attachments(request, related_object, files, description=""):
    content_type = ContentType.objects.get_for_model(related_object)
    uploaded_by = _current_user(request)
    attachments = []
    for file_obj in files:
        error = _upload_error(file_obj)
        if error:
            return attachments, error
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        attachments.append(
            Attachment.objects.create(
                file=file_obj,
                file_type=_file_type(file_obj),
                uploaded_by=uploaded_by,
                content_type=content_type,
                object_id=related_object.pk,
                description=description,
            )
        )
    return attachments, None


def _attachments_for(related_object):
    content_type = ContentType.objects.get_for_model(related_object)
    return Attachment.objects.filter(content_type=content_type, object_id=related_object.pk)


# Formatting Helpers

def _format_client(client):
    return {
        "id": str(client.id),
        "userId": str(client.user_id),
        "inn": client.inn,
        "name": client.company_name,
        "category": client.category,
        "region": client.region.name if client.region else "",
        "city": client.city,
        "contact": client.contact_name,
        "phone": client.phone,
        "distributorId": str(client.distributor_id),
        "managerId": str(client.manager_id) if client.manager_id else None,
        "registrationSource": client.registration_source,
        "status": client.status,
        "partnerStatus": client.partner_status,
        "referralCode": client.referral_code,
        "totalPurchases": float(
            Purchase.objects
            .filter(client=client, status="verified")
            .aggregate(total=Sum("total_amount"))["total"]
            or 0
        ),
        "createdAt": client.created_at.isoformat(),
    }


def _format_distributor(distributor):
    return {
        "id": str(distributor.id),
        "userId": str(distributor.user_id) if distributor.user_id else None,
        "name": distributor.name,
        "inn": distributor.inn,
        "regions": distributor.regions,
        "phone": distributor.phone,
        "email": distributor.email,
        "isActive": distributor.is_active,
    }


def _format_store(store):
    return {
        "id": str(store.id),
        "name": store.name,
        "address": store.address,
        "isActive": store.is_active,
        "createdAt": store.created_at.isoformat(),
    }


def _format_product(product, client=None):
    """Товар для API. С ``client`` цена пересчитывается под его ранг."""
    payload = {
        "id": str(product.id),
        "distributorId": str(product.distributor_id),
        "sku": product.sku,
        "wbArticle": product.wb_article or None,
        "groupName": product.group_name or None,
        "name": product.name,
        "category": product.category,
        "brand": product.brand,
        "description": product.description or None,
        "color": product.color or None,
        "barcode": product.barcode or None,
        "images": product.images or [],
        "videoUrl": product.video_url or None,
        "volume": float(product.volume),
        "weight": float(product.weight),
        "packageHeight": float(product.package_height),
        "packageLength": float(product.package_length),
        "packageWidth": float(product.package_width),
        "tnved": product.tnved or None,
        "vatRate": product.vat_rate or None,
        "price": float(product.price),
        "quantity": product.quantity,
        "status": product.status,
        "updatedAt": product.updated_at.isoformat(),
    }
    if client is not None:
        # Клиент видит цену своего ранга; базовую отдаём рядом, чтобы в
        # каталоге можно было показать зачёркнутую цену и размер скидки.
        details = price_details(client, product)
        payload["price"] = float(details["price"])
        payload["basePrice"] = float(details["base_price"])
        payload["discountPercent"] = float(details["discount_percent"])
        payload["hasDiscount"] = details["has_discount"]
    return payload


def _format_attachment(item):
    return {
        "id": str(item.id),
        "url": item.file.url if item.file else None,
        "name": item.file.name.split("/")[-1] if item.file else "",
        "fileType": item.file_type,
        "uploadedBy": str(item.uploaded_by_id) if item.uploaded_by_id else None,
        "uploadedAt": item.uploaded_at.isoformat(),
        "description": item.description or None,
    }


def _format_order_item(item):
    # id нужен оператору, чтобы отправить корректировку (adjust_order ждёт itemId),
    # availableQuantity — чтобы прямо в позиции было видно, хватает ли остатка.
    product = item.product
    if product is None:
        available = None
    elif product.status == "onOrder":
        available = None  # товар под заказ — остаток не ограничивает
    else:
        available = product.quantity

    return {
        "id": str(item.id),
        "productId": str(item.product_id) if item.product_id else None,
        "sku": item.sku,
        "name": item.name,
        "category": item.category,
        "quantity": item.quantity,
        "volume": float(item.volume),
        "price": float(item.price),
        "brand": item.brand,
        "availableQuantity": available,
    }


def _format_order(order):
    items = [_format_order_item(item) for item in order.items.select_related("product")]
    def _get_courier_name(c):
        if not c:
            return None
        full_name = f"{c.first_name} {c.last_name}".strip()
        return full_name if full_name else f"Курьер {c.username}"

    return {
        "id": str(order.id),
        "clientId": str(order.client_id),
        "clientName": order.client.company_name,
        "clientInn": order.client.inn,
        "distributorId": str(order.distributor_id),
        "storeId": str(order.store_id) if order.store_id else None,
        "storeName": order.store.name if order.store else "Не назначен",
        "documentNumber": f"ORD-{order.id:05d}",
        "date": order.created_at.isoformat(),
        "totalAmount": float(order.total_amount),
        "status": order.status,
        "deliveryMethod": order.delivery_method,
        "comment": order.comment,
        "rejectionReason": order.rejection_reason or None,
        "courierId": str(order.courier_id) if order.courier_id else None,
        "courierName": _get_courier_name(order.courier),
        "estimatedDeliveryDate": order.estimated_delivery_date.isoformat() if order.estimated_delivery_date else None,
        "createdAt": order.created_at.isoformat(),
        "confirmedAt": order.confirmed_at.isoformat() if order.confirmed_at else None,
        "paidAt": order.paid_at.isoformat() if order.paid_at else None,
        "shippedAt": order.shipped_at.isoformat() if order.shipped_at else None,
        # Клиент может нажать «Оплатить» только для этих статусов
        "isPayable": order.status in Order.PAYABLE_STATUSES,
        # Сколько бонусов можно бросить в этот заказ — экран оплаты показывает
        # это до нажатия «Оплатить».
        "bonusAvailable": float(bonus_balance(order.client)),
        "bonusApplied": float(_bonus_applied(order)),
        "adjustments": [_format_adjustment(a) for a in order.adjustments.all()],
        "pendingPayment": _format_pending_payment(order),
        "items": items,
    }


def _bonus_applied(order):
    """Сколько бонусов уже списано в этот заказ (за вычетом возвратов)."""
    total = order.bonus_transactions.filter(kind__in=["order", "refund"]).aggregate(
        total=Sum("amount")
    )["total"]
    return abs(Decimal(total or 0))


def _format_adjustment(adj):
    return {
        "id": str(adj.id),
        "originalItems": adj.original_items,
        "adjustedItems": adj.adjusted_items,
        "reason": adj.reason or None,
        "createdAt": adj.created_at.isoformat(),
    }


def _format_pending_payment(order):
    payment = order.payments.filter(status__in=["pending", "waiting_for_capture"]).first()
    if not payment or not payment.confirmation_url:
        return None
    return {
        "id": str(payment.id),
        "status": payment.status,
        "confirmationUrl": payment.confirmation_url,
    }


def _format_purchase_item(item):
    return {
        "sku": item.sku,
        "name": item.name,
        "category": item.category,
        "quantity": item.quantity,
        "volume": float(item.volume),
        "price": float(item.price),
        "brand": item.brand,
    }


def _format_purchase(purchase):
    return {
        "id": str(purchase.id),
        "clientId": str(purchase.client_id),
        "clientName": purchase.client.company_name,
        "clientInn": purchase.client.inn,
        "distributorId": str(purchase.distributor_id),
        "documentNumber": purchase.document_number,
        "date": purchase.date.isoformat(),
        "totalAmount": float(purchase.total_amount),
        "status": purchase.status,
        "documentUrl": purchase.document_file.url if purchase.document_file else (purchase.document_url or None),
        "documentHash": purchase.document_hash or None,
        "rejectionReason": purchase.rejection_reason or None,
        "createdAt": purchase.created_at.isoformat(),
        "items": [_format_purchase_item(item) for item in purchase.items.all()],
        "attachments": [_format_attachment(item) for item in _attachments_for(purchase)],
    }


def _format_recipe_material(item):
    return {
        "id": str(item.id),
        "sku": item.sku,
        "quantity": float(item.quantity),
        "unit": item.unit,
        "comment": item.comment or None,
        "version": item.version,
    }


def _format_color_request(item):
    materials = [_format_recipe_material(m) for m in item.materials.all()]
    courier_tasks = [_format_courier_task(t) for t in item.courier_tasks.all()]
    now = timezone.now()
    is_overdue = item.sla_deadline and now > item.sla_deadline and item.status != "delivered"
    return {
        "id": str(item.id),
        "clientId": str(item.client_id),
        "clientName": item.client.company_name,
        "carBrand": item.car_brand,
        "carModel": item.car_model,
        "carYear": item.car_year or None,
        "vin": item.vin,
        "colorCode": item.color_code,
        "colorName": item.color_name,
        # Тип покрытия: от него зависят рецепт и цена добора.
        "paintType": item.paint_type,
        "paintTypeLabel": item.get_paint_type_display(),
        "paintTypeNote": item.paint_type_note or None,
        "urgent": item.urgent,
        "comment": item.comment or None,
        "transferMethod": item.transfer_method,
        "pickupAddress": item.pickup_address or None,
        "pickupTime": item.pickup_time.isoformat() if item.pickup_time else None,
        # «Курьер может приехать до» — дедлайн для выезда, формат HH:MM.
        "courierArriveUntil": item.courier_arrive_until.strftime("%H:%M") if item.courier_arrive_until else None,
        "contactPerson": item.contact_person or None,
        "contactPhone": item.contact_phone or None,
        "slaDeadline": item.sla_deadline.isoformat() if item.sla_deadline else None,
        "isOverdue": is_overdue,
        "assignedDistributorId": str(item.assigned_distributor_id) if item.assigned_distributor_id else None,
        "assignedStation": item.assigned_station or None,
        "status": item.status,
        "statusHistory": item.status_history or [],
        "recipe": item.recipe or None,
        "materials": materials,
        "courierTasks": courier_tasks,
        "createdAt": item.created_at.isoformat(),
        "attachments": [_format_attachment(attachment) for attachment in _attachments_for(item)],
    }


def _courier_phone(courier):
    """Телефон курьера, чтобы клиент мог позвонить.

    Курьеры регистрируются по номеру телефона — он же username (так же это
    поле отдаётся в distributor_couriers). Служебные логины без цифр наружу
    не отдаём: звонить по ним всё равно некуда.
    """
    if not courier:
        return None
    username = (courier.username or "").strip()
    digits = sum(character.isdigit() for character in username)
    return username if digits >= 6 else None


def _format_courier_task(item):
    if not item:
        return None
    return {
        "id": str(item.id),
        "clientId": str(item.client_id),
        "clientName": item.client.company_name,
        "clientInn": item.client.inn,
        "orderId": str(item.order_id) if item.order_id else None,
        "colorRequestId": str(item.color_request_id) if item.color_request_id else None,
        "courierId": str(item.courier_id) if item.courier_id else None,
        "courierName": item.courier.get_full_name() or item.courier.username if item.courier else None,
        "courierPhone": _courier_phone(item.courier),
        "taskType": item.task_type,
        "typeDisplay": item.get_task_type_display(),
        "address": item.address,
        "timeSlot": item.time_slot,
        # Контакты и время клиент присылает при создании заявки — без них
        # в карточке доставки нечего показать, кроме адреса.
        "contactName": item.contact_name or None,
        "contactPhone": item.contact_phone or None,
        "scheduledTime": item.scheduled_time.isoformat() if item.scheduled_time else None,
        "carDescription": item.car_description or None,
        "status": item.status,
        "statusDisplay": item.get_status_display(),
        "assignedCourierId": str(item.courier_id) if item.courier_id else None,
        "photoProof": item.proof_photo.url if item.proof_photo else None,
        "comment": item.comment,
        "courierComment": item.courier_comment,
        "statusHistory": item.status_history or [],
        "createdAt": item.created_at.isoformat(),
        "attachments": [_format_attachment(attachment) for attachment in _attachments_for(item)],
    }


def _format_ticket(item):
    return {
        "id": str(item.id),
        "clientId": str(item.client_id),
        "clientName": item.client.company_name,
        "question": item.question,
        "category": item.category,
        "risk": item.risk,
        "status": item.status,
        "aiDraftAnswer": item.ai_draft_answer or None,
        "aiAnswer": item.ai_answer or None,
        "expertAnswer": item.expert_answer or None,
        "linkedKnowledgeCardId": str(item.linked_knowledge_card_id) if item.linked_knowledge_card_id else None,
        "similarCases": item.similar_cases or [],
        "createdAt": item.created_at.isoformat(),
        "updatedAt": item.updated_at.isoformat(),
        "attachments": [_format_attachment(a) for attachment in _attachments_for(item)],
    }


def _format_knowledge_card(item):
    return {
        "id": str(item.id),
        "title": item.title or item.problem,
        "category": item.category,
        "problem": item.problem,
        "causes": item.causes or "",
        "solution": item.solution,
        "skus": item.skus or [],
        "restrictions": item.restrictions or None,
        "status": item.status,
        "isApproved": item.status == "approved",
        "approvingExpert": item.expert_author.username if item.expert_author else "System",
        "revisionHistory": item.revision_history or [],
        "createdAt": item.created_at.isoformat(),
        "updatedAt": item.updated_at.isoformat(),
    }


def _format_notification(item):
    return {
        "id": str(item.id),
        "title": item.title,
        "body": item.body,
        "type": item.type,
        "relatedLink": item.related_link,
        "isRead": item.is_read,
        "createdAt": item.created_at.isoformat(),
    }


def _format_referral(item):
    return {
        "id": str(item.id),
        "inviterId": str(item.inviter_id),
        "inviteeInn": item.invitee_inn,
        "inviteeName": item.invitee_name,
        "region": item.region,
        "isRegistered": item.is_registered,
        "hasPurchase": item.has_purchase,
        "purchaseAmount": float(item.purchase_amount),
        "conditionMet": item.condition_met,
        # Подарок показываем только после согласования: до него клиенту нельзя
        # обещать скидку или отсрочку, это деньги дистрибьютора.
        "gift": item.gift if item.gift_is_issued else None,
        "giftStatus": item.gift_status,
        "giftComment": item.gift_comment or None,
        # auto · pending · confirmed · declined. Пригласивший должен видеть,
        # что его заявка ещё не подтверждена приглашённым и подарка не будет.
        "confirmation": item.confirmation,
        "createdAt": item.created_at.isoformat(),
    }


def _format_pending_claim(referral):
    """Заявка, которую приглашённому предлагают подтвердить.

    Отдаём только название пригласившего: приглашённому этого хватит, чтобы
    узнать коллегу, а лишние данные чужой компании тут ни к чему.
    """
    return {
        "id": str(referral.id),
        "inviterName": referral.inviter.company_name,
        "inviterCity": referral.inviter.city or "",
    }


def _strict_inn_enabled() -> bool:
    """Проверять ли ИНН по контрольной сумме при заявке на приглашение.

    По умолчанию да: настоящие ИНН её всегда проходят, а опечатка иначе
    оборачивается заявкой, которая молча висит вечно. Выключается через
    REFERRAL_STRICT_INN = False — это нужно на стенде, где клиенты заведены
    с выдуманными номерами вроде 7701234567.
    """
    return bool(getattr(settings, "REFERRAL_STRICT_INN", True))


def _pending_claim_for(client):
    """Неподтверждённая заявка на этого клиента, если она есть."""
    return (
        Referral.objects.filter(invitee_inn=client.inn, confirmation="pending")
        .select_related("inviter")
        .order_by("created_at")
        .first()
    )


# Autoservice Views

@require_GET
def health(_request):
    return JsonResponse({"status": "ok", "service": "autoterra-api"})


@csrf_exempt
@require_POST
def register(request):
    payload = _json(request)
    # Пароль в лог не пишем: payload логировался целиком, и пароли клиентов
    # оказывались в открытом виде в логах сервера.
    logger.info(
        "Регистрация: телефон=%s, ИНН=%s, регион=%s",
        payload.get("username"), payload.get("inn"), payload.get("region_id"),
    )
    serializer = RegistrationSerializer(payload)
    if not serializer.is_valid():
        logger.info("Регистрация отклонена валидацией: %s", serializer.errors)
        # Возвращаем detail для фронтенда, чтобы он мог показать ошибку
        first_err_msg = "Ошибка валидации"
        if serializer.errors:
            # Извлекаем первое сообщение об ошибке
            field = next(iter(serializer.errors))
            first_err_msg = f"{field}: {serializer.errors[field]}"
            if isinstance(serializer.errors[field], list):
                first_err_msg = serializer.errors[field][0]
            else:
                first_err_msg = str(serializer.errors[field])
        
        return JsonResponse({"detail": first_err_msg, "errors": serializer.errors}, status=400)
    
    validated_data = serializer.validated_data
    username = validated_data['username']
    password = validated_data['password']
    inn = validated_data['inn']
    region = validated_data['region']
    company_name = validated_data['company_name']
    contact_name = validated_data['contact_name']
    store_address = validated_data['store_address']
    
    # 1. Проверка уникальности номера телефона
    if User.objects.filter(username=username).exists():
        return JsonResponse({
            "detail": f"Номер телефона {username} уже используется. Пожалуйста, войдите в аккаунт или используйте другой номер.", 
            "code": "phone_duplicate"
        }, status=409)

    # 2. Проверка уникальности ИНН (локально в текущем регионе)
    if ClientProfile.objects.filter(inn=inn, region=region).exists():
        return JsonResponse({
            "detail": f"Организация с ИНН {inn} уже зарегистрирована в регионе {region.name}. Повторная регистрация в одном и том же регионе запрещена.", 
            "code": "inn_duplicate"
        }, status=409)

    # Если ИНН есть в других регионах, помечаем как филиал (статус "на проверке")
    is_branch = ClientProfile.objects.filter(inn=inn).exclude(region=region).exists()
    status = "under_review" if is_branch else "new"

    try:
        with transaction.atomic():
            # Создаем пользователя
            user = User.objects.create_user(username=username, password=password)
            
            # Создаем профиль
            client = ClientProfile.objects.create(
                user=user,
                inn=inn,
                company_name=company_name,
                region=region,
                status=status,
                phone=username,
                city=region.name,
                contact_name=contact_name,
                registration_source="app",
                distributor=region.distributor,
                manager=region.manager
            )

            # Создаем основной магазин/точку для клиента
            Store.objects.create(
                client=client,
                name=company_name,
                address=store_address,
                is_active=True
            )
            
            token = secrets.token_hex(24)
            AuthToken.objects.create(key=token, user=user)

            # Пришёл по приглашению — связываем с пригласившим (п. 7 ТЗ, шаг 2).
            _link_referral(client, payload.get("referralCode") or payload.get("ref"))

    except IntegrityError as e:
        # Резервный обработчик на случай гонки условий
        return JsonResponse({
            "detail": "Ошибка уникальности данных: возможно, ИНН или телефон были зарегистрированы только что другим пользователем.",
            "code": "integrity_error"
        }, status=409)
    except Exception as e:
        return JsonResponse({
            "detail": f"Внутренняя ошибка сервера при регистрации: {str(e)}",
            "code": "server_error"
        }, status=500)

    # Письмо — уже после коммита: SMTP не должен держать транзакцию открытой,
    # и уведомление не должно уйти по клиенту, чья запись откатилась.
    _send_registration_email(client)

    # Кто-то мог заявить это СТО вручную ещё до регистрации. Такая заявка не
    # даёт права на подарок, пока клиент сам её не подтвердит, — показываем её
    # сразу, пока человек ещё в потоке регистрации.
    pending_claim = _pending_claim_for(client)

    return JsonResponse({
        "status": "success",
        "token": token,
        "client": _format_client(client),
        "requires_approval": status == "under_review",
        "pendingReferral": _format_pending_claim(pending_claim) if pending_claim else None,
    }, status=201)


@csrf_exempt
@require_POST
def login(request):
    payload = _json(request)
    login_input = (payload.get("phone") or "").strip()
    password = payload.get("password") or ""
    
    # 1. Try to authenticate with raw input (useful for text logins like 'admin')
    user = authenticate(username=login_input, password=password)
    
    # 2. If fails, try normalizing as phone and authenticate again
    if user is None:
        normalized = _normalize_phone(login_input)
        if normalized and normalized != login_input:
            user = authenticate(username=normalized, password=password)
            
    # 3. Final check (fallback check for users without proper password setup but exist in DB)
    if user is None:
        # Check if login_input or normalized matches username
        matches = [login_input]
        normalized = _normalize_phone(login_input)
        if normalized and normalized != login_input:
            matches.append(normalized)
            
        user = User.objects.filter(username__in=matches).first()
        if user is None or not user.check_password(password):
            return JsonResponse({"detail": "Неверный логин или пароль"}, status=401)

    token = secrets.token_hex(24)
    AuthToken.objects.create(key=token, user=user)

    role = getattr(user, "profile", None).role if hasattr(user, "profile") else "client"
    
    if role == "client":
        try:
            client = user.client_profile
            return JsonResponse({"token": token, "user": {"id": str(user.id), "phone": client.phone, "role": "autoservice", "status": client.status}})
        except ClientProfile.DoesNotExist:
            return JsonResponse({"detail": "Профиль клиента не создан"}, status=403)
            
    if role == "distributor":
        distributor = getattr(user, "distributor_profile", None)
        if distributor is None:
            return JsonResponse({"detail": "Профиль дистрибьютора не создан"}, status=403)
        return JsonResponse({"token": token, "user": {"id": str(user.id), "phone": user.username, "role": "distributor", "status": "active", "distributor": _format_distributor(distributor)}})

    if role == "courier":
        return JsonResponse({"token": token, "user": {"id": str(user.id), "phone": user.username, "role": "courier", "status": "active"}})

    if role == "ai_expert":
        return JsonResponse({"token": token, "user": {"id": str(user.id), "phone": user.username, "role": "expert", "status": "active"}})

    if role == "manager" or role == "admin":
        return JsonResponse({"token": token, "user": {"id": str(user.id), "phone": user.username, "role": role, "status": "active"}})

    return JsonResponse({"detail": "Неизвестная роль"}, status=403)


@csrf_exempt
@require_POST
def password_reset(request):
    payload = _json(request)
    login_input = (payload.get("phone") or payload.get("username") or "").strip()
    inn = _normalize_inn(payload.get("inn"))
    new_password = payload.get("new_password") or payload.get("password") or ""

    if not login_input:
        return JsonResponse({"detail": "Введите телефон или логин"}, status=400)
    if len(new_password) < 8:
        return JsonResponse({"detail": "Пароль должен быть не менее 8 символов"}, status=400)
    if len(inn) not in (10, 12):
        return JsonResponse({"detail": "ИНН должен состоять из 10 или 12 цифр"}, status=400)

    matches = [login_input]
    normalized = _normalize_phone(login_input)
    if normalized and normalized != login_input:
        matches.append(normalized)

    user = User.objects.filter(username__in=matches).first()
    if user is None:
        return JsonResponse({"detail": "Аккаунт с такими данными не найден"}, status=404)

    role = getattr(user, "profile", None).role if hasattr(user, "profile") else "client"
    account_inn = None
    if role == "client":
        client = getattr(user, "client_profile", None)
        account_inn = client.inn if client else None
    elif role == "distributor":
        distributor = getattr(user, "distributor_profile", None)
        account_inn = distributor.inn if distributor else None

    if account_inn != inn:
        return JsonResponse({"detail": "Аккаунт с такими данными не найден"}, status=404)

    user.set_password(new_password)
    user.save(update_fields=["password"])
    user.auth_tokens.all().delete()

    return JsonResponse({"status": "success", "detail": "Пароль обновлен"})


@require_GET
def me(request):
    user = _current_user(request)
    if user is None:
        return JsonResponse({"detail": "Unauthorized"}, status=401)
        
    profile = getattr(user, "profile", None)
    role = profile.role if profile else "client"
    
    if role == "client":
        try:
            client = user.client_profile
            return JsonResponse({
                "id": str(user.id), 
                "phone": client.phone, 
                "role": "autoservice", 
                "status": client.status,
                "client": _format_client(client), 
                "distributor": _format_distributor(client.distributor)
            })
        except ClientProfile.DoesNotExist:
            return JsonResponse({"detail": "Профиль клиента не создан"}, status=403)

    if role == "distributor":
        distributor = getattr(user, "distributor_profile", None)
        if distributor is None:
            return JsonResponse({"detail": "Профиль дистрибьютора не создан"}, status=403)
        return JsonResponse({
            "id": str(user.id), 
            "phone": user.username, 
            "role": "distributor", 
            "status": "active", 
            "distributor": _format_distributor(distributor)
        })

    if role == "courier":
        return JsonResponse({
            "id": str(user.id), 
            "phone": user.username, 
            "role": "courier", 
            "status": "active"
        })

    if role == "ai_expert":
        approved_cards = KnowledgeCard.objects.filter(expert_author=user, status="approved").count()
        answered_tickets = ExpertTicket.objects.filter(expert_author=user, status="expertAnswered").count()
        
        return JsonResponse({
            "id": str(user.id), 
            "username": user.username,
            "email": user.email,
            "phone": user.username, # Assuming phone is username
            "role": "expert", 
            "status": "active",
            "stats": {
                "approvedCards": approved_cards,
                "answeredTickets": answered_tickets,
                "rating": float(profile.rating),
            },
            "specialty": profile.specialty or "Эксперт AutoTerra",
            "expertId": f"EX-{user.id + 77000 if isinstance(user.id, int) else user.id}",
            "region": ", ".join(r.name for r in user.managed_regions.all()) or "Все регионы",
            "dateJoined": user.date_joined.strftime("%d.%m.%Y")
        })

    if role == "manager" or role == "admin":
        return JsonResponse({
            "id": str(user.id), 
            "phone": user.username, 
            "role": role, 
            "status": "active"
        })

    return JsonResponse({"detail": "Профиль не найден"}, status=403)


@require_GET
def dashboard(request):
    client, err = _require_client(request)
    if err:
        return err
    purchases = client.purchases.prefetch_related("items").order_by("-date")[:2]
    color_requests = client.color_requests.exclude(status="delivered").order_by("-created_at")[:2]
    referrals = client.referrals.all()
    return JsonResponse({
        "client": _format_client(client),
        "distributor": _format_distributor(client.distributor),
        "unreadCount": client.user.notifications.filter(is_read=False).count(),
        "recentPurchases": [_format_purchase(item) for item in purchases],
        "activeColorRequests": [_format_color_request(item) for item in color_requests],
        # Счётчики для профиля: там показывается, скольких клиент уже привёл.
        # Полный список и код — на отдельном экране (/api/referrals/).
        "referralSummary": {
            "invitedCount": referrals.count(),
            "buyersCount": referrals.filter(has_purchase=True).count(),
            # Как и на экране рефералов: выданным считается только согласованный.
            "giftCount": referrals.filter(condition_met=True, gift_status="approved").count(),
        },
        # Бонусы уменьшают сумму к оплате в ЮKassa, поэтому баланс нужен и на
        # главной, и в профиле.
        "bonusBalance": float(bonus_balance(client)),
        # Если при регистрации подтверждение пропустили, спросим ещё раз с
        # главной: иначе заявка так и повиснет неподтверждённой.
        "pendingReferral": (
            _format_pending_claim(claim) if (claim := _pending_claim_for(client)) else None
        ),
    })


@csrf_exempt
def stores(request):
    client, err = _require_client(request)
    if err:
        return err

    if request.method == "GET":
        qs = client.stores.filter(is_active=True)
        return JsonResponse(paginated_response(request, qs, _format_store))

    if request.method == "POST":
        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({"detail": "Неверный JSON"}, status=400)
        name = (body.get("name") or "").strip()
        address = (body.get("address") or "").strip()
        if not name:
            return JsonResponse({"detail": "Название обязательно"}, status=400)
        if not address:
            return JsonResponse({"detail": "Адрес обязателен"}, status=400)
        store = Store.objects.create(client=client, name=name, address=address)
        return JsonResponse({"store": _format_store(store)}, status=201)

    return JsonResponse({"detail": "Method not allowed"}, status=405)


@csrf_exempt
def store_detail(request, store_id):
    client, err = _require_client(request)
    if err:
        return err
    store = get_object_or_404(Store, id=store_id, client=client)

    if request.method in ("PUT", "PATCH"):
        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({"detail": "Неверный JSON"}, status=400)
        name = (body.get("name") or "").strip()
        address = (body.get("address") or "").strip()
        if not name:
            return JsonResponse({"detail": "Название обязательно"}, status=400)
        if not address:
            return JsonResponse({"detail": "Адрес обязателен"}, status=400)
        store.name = name
        store.address = address
        store.save(update_fields=["name", "address"])
        return JsonResponse({"store": _format_store(store)})

    if request.method == "DELETE":
        store.is_active = False
        store.save(update_fields=["is_active"])
        return JsonResponse({"ok": True})

    return JsonResponse({"detail": "Method not allowed"}, status=405)


@require_GET
def products(request):
    client, err = _require_client(request)
    if err:
        return err
    qs = Product.objects.filter(distributor=client.distributor, is_active=True)
    category = (request.GET.get("category") or "").strip()
    search = (request.GET.get("search") or "").strip()
    brand = (request.GET.get("brand") or "").strip()
    if category:
        qs = qs.filter(category=category)
    if brand:
        qs = qs.filter(brand=brand)
    if request.GET.get("inStock") == "true":
        # «Под заказ» тоже доступен к заказу, хотя остаток нулевой.
        qs = qs.filter(Q(quantity__gt=0) | Q(status="onOrder"))
    if search:
        qs = _search_products(qs, search)
    # order_by() сбрасывает Meta.ordering ("category", "name"): иначе "name"
    # попадает в SELECT ради сортировки и DISTINCT перестаёт схлопывать
    # одинаковые категории.
    categories = list(
        Product.objects.filter(distributor=client.distributor, is_active=True)
        .order_by("category")
        .values_list("category", flat=True)
        .distinct()
    )
    return JsonResponse(paginated_response(
        request,
        qs.order_by("category", "name"),
        lambda product: _format_product(product, client),
        extra={"categories": categories},
    ))


@require_GET
def order_config(request):
    client, err = _require_client(request)
    if err:
        return err
    stores_qs = client.stores.filter(is_active=True)
    products_qs = Product.objects.filter(distributor=client.distributor, is_active=True)
    # Здесь только справочники для формы заказа. Сам ассортимент отдаёт
    # постраничный /products/ — раньше каталог целиком (сотни позиций) уезжал
    # одним ответом на каждое открытие экрана.
    categories = list(
        products_qs.order_by("category").values_list("category", flat=True).distinct()
    )
    brands = list(
        products_qs.order_by("brand").values_list("brand", flat=True).distinct()
    )
    return JsonResponse({
        "client": _format_client(client),
        "distributor": _format_distributor(client.distributor),
        "stores": [_format_store(s) for s in stores_qs],
        "categories": categories,
        "brands": brands,
    })


@require_GET
def orders(request):
    client, err = _require_client(request)
    if err:
        return err
    qs = client.orders.select_related("store", "distributor").prefetch_related("items").order_by("-created_at")
    return JsonResponse(paginated_response(request, qs, _format_order))


def order_detail(request, order_id):
    """Один заказ. Нужен для перехода из письма/пуша по ссылке /orders/<id>.

    Доступ у клиента-владельца и у дистрибьютора, которому заказ адресован.
    """
    user = _current_user(request)
    if user is None:
        return JsonResponse({"detail": "Unauthorized"}, status=401)

    order = (
        Order.objects.select_related("client", "store", "distributor")
        .prefetch_related("items__product", "adjustments", "payments")
        .filter(id=order_id)
        .first()
    )
    if not order:
        return JsonResponse({"detail": "Заказ не найден"}, status=404)

    role = getattr(getattr(user, "profile", None), "role", "client")
    if role == "client":
        allowed = getattr(getattr(user, "client_profile", None), "id", None) == order.client_id
    elif role in ("distributor", "operator"):
        distributor = getattr(user, "distributor_profile", None)
        allowed = distributor is not None and distributor.id == order.distributor_id
    else:
        allowed = role in ("admin", "manager")

    if not allowed:
        return JsonResponse({"detail": "Нет доступа к заказу"}, status=403)

    return JsonResponse({"order": _format_order(order)})


@csrf_exempt
@require_POST
def create_order(request):
    client, err = _require_client(request)
    if err:
        return err
    payload = _json(request)
    store_id = payload.get("storeId")
    items = payload.get("items") or []
    
    if not items:
        return JsonResponse({"detail": "Добавьте товары в заказ"}, status=400)
        
    store = None
    if store_id:
        store = Store.objects.filter(id=store_id, client=client, is_active=True).first()
        if store is None:
            return JsonResponse({"detail": "Указанный магазин не найден"}, status=400)

    with transaction.atomic():
        order = Order.objects.create(
            client=client, 
            store=store, 
            distributor=client.distributor, 
            delivery_method=payload.get("deliveryMethod", "courier"),
            comment=(payload.get("comment") or "").strip()
        )
        for raw in items:
            product = Product.objects.filter(id=raw.get("productId"), distributor=client.distributor, is_active=True).first()
            if product:
                qty = int(raw.get("quantity") or 1)
                
                # STOCK VALIDATION
                # If NOT onOrder, check if we have enough quantity
                if product.status != "onOrder" and product.quantity < qty:
                    transaction.set_rollback(True)
                    return JsonResponse({
                        "detail": f"Недостаточно товара '{product.name}' на складе. Доступно: {product.quantity}"
                    }, status=400)

                OrderItem.objects.create(
                    order=order, 
                    product=product, 
                    sku=product.sku, 
                    name=product.name, 
                    category=product.category, 
                    brand=product.brand, 
                    volume=product.volume,
                    price=price_for_client(client, product),
                    quantity=qty
                )
                
                # Update quantity
                product.quantity = max(0, product.quantity - qty)
                if product.quantity > 5:
                    product.status = "inStock"
                elif product.quantity > 0:
                    product.status = "low"
                else:
                    # If it was already onOrder, keep it onOrder (allowing further backorders)
                    # Otherwise, it becomes outOfStock
                    if product.status != "onOrder":
                        product.status = "outOfStock"
                product.save(update_fields=["quantity", "status"])

    _notify(
        getattr(client.distributor, "user", None),
        "Новый заказ",
        f"{client.company_name}: новый заказ ({order.items.count()} поз.)",
        "order",
        link="/distributor",
    )
    _send_order_email(order)
    return JsonResponse({"order": _format_order(order)}, status=201)


@csrf_exempt
@require_POST
def cancel_order(request, order_id):
    client, err = _require_client(request)
    if err:
        return err
    
    order = client.orders.filter(id=order_id).first()
    if not order:
        return JsonResponse({"detail": "Заказ не найден"}, status=404)

    # Клиент может отменить заказ только до оплаты (new / confirmed / adjusted)
    if not order.can_transition_to("cancelled"):
        return JsonResponse(
            {"detail": "Нельзя отменить заказ на текущем этапе"}, status=400
        )

    old_status = order.status
    with transaction.atomic():
        _restore_order_stock(order)
        order.status = "cancelled"
        order.rejection_reason = "Отменено клиентом"
        order.save(update_fields=["status", "rejection_reason"])

    _notify_operator_order(order, "Заказ отменён", f"{order.client.company_name} отменил заказ ORD-{order.id:05d}")
    _send_order_status_email(order, old_status, "cancelled")

    return JsonResponse({"status": "success", "order": _format_order(order)})


# ═══════════════════════════════════════════════════════════════════════════════
#  Безопасный поток заказа: подтверждение / корректировка / оплата / отправка
# ═══════════════════════════════════════════════════════════════════════════════

def _restore_order_stock(order):
    """Вернуть остатки на склад по всем позициям заказа."""
    for item in order.items.select_related("product").all():
        product = item.product
        if product is None:
            continue
        product.quantity += item.quantity
        if product.quantity > 5:
            product.status = "inStock"
        elif product.quantity > 0:
            product.status = "low"
        product.save(update_fields=["quantity", "status"])


def _search_products(qs, query):
    """Поиск по товарам: Elasticsearch, если настроен, иначе поиск в БД."""
    from api.services.search import search_products

    return search_products(qs, query)


def _stock_shortages(items):
    """Список позиций, где запрошенное количество превышает остаток.

    ``items`` — iterable из (product, requested_qty). Возвращает список dict-ов
    для предупреждения оператора.
    """
    shortages = []
    for product, requested in items:
        available = product.quantity if product.status != "onOrder" else requested
        if product.status != "onOrder" and requested > product.quantity:
            shortages.append({
                "productId": str(product.id),
                "name": product.name,
                "sku": product.sku,
                "requested": requested,
                "available": product.quantity,
            })
    return shortages


def _notify_client_order(order, title, body):
    _notify(getattr(order.client, "user", None), title, body, "order", link=f"/orders/{order.id}")


def _notify_operator_order(order, title, body):
    _notify(getattr(order.distributor, "user", None), title, body, "order", link="/distributor")


def _client_email_list(order):
    email = getattr(getattr(order.client, "user", None), "email", "") or ""
    return [email] if email else []


def _items_snapshot(order):
    """JSON-снимок текущих позиций заказа (для истории корректировок).

    Читаем отдельным запросом, а не через ``order.items.all()``: заказ приходит
    с ``prefetch_related("items")``, и после правок кэш отдал бы старый состав —
    снимок «стало» совпал бы со снимком «было».
    """
    return [
        {
            "productId": str(item.product_id),
            "sku": item.sku,
            "name": item.name,
            "price": float(item.price),
            "quantity": item.quantity,
            "total": float(item.total),
        }
        for item in OrderItem.objects.filter(order=order).order_by("id")
    ]


@csrf_exempt
@require_POST
def confirm_order(request, order_id):
    """Оператор подтверждает заказ как есть: new/adjusted → confirmed."""
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    order = _scope_orders(distributor, is_admin).filter(id=order_id).first()
    if not order:
        return JsonResponse({"detail": "Заказ не найден"}, status=404)
    if not order.can_transition_to("confirmed"):
        return JsonResponse({"detail": f"Нельзя подтвердить заказ в статусе «{order.get_status_display()}»"}, status=400)

    # Предупреждение о нехватке остатков (не блокирует — оператор решает сам,
    # но по умолчанию не даём подтвердить дефицитный заказ без ?force=1)
    shortages = _stock_shortages(
        [(item.product, item.quantity) for item in order.items.select_related("product").all() if item.product]
    )
    if shortages and request.GET.get("force") != "1":
        return JsonResponse({
            "detail": "Недостаточно остатков по некоторым позициям. Скорректируйте заказ или подтвердите принудительно (force=1).",
            "shortages": shortages,
        }, status=409)

    old_status = order.status
    order.status = "confirmed"
    order.confirmed_at = timezone.now()
    order.save(update_fields=["status", "confirmed_at"])

    _log_audit(request, f"Order confirm: {old_status} -> confirmed", order)
    _notify_client_order(order, "Заказ подтверждён", f"Заказ ORD-{order.id:05d} подтверждён. Сумма: {order.total_amount} ₽. Можно оплатить.")
    _send_order_status_email(order, old_status, "confirmed", recipients=_client_email_list(order))
    return JsonResponse({"order": _format_order(order)})


@csrf_exempt
@require_POST
def adjust_order(request, order_id):
    """Оператор корректирует состав заказа: new → adjusted.

    payload: {
        "items": [{"itemId": 12, "quantity": 3}, ...],   # правка существующих
        "newItems": [{"productId": 5, "quantity": 2}],   # добавление позиций
        "reason": "..."
    }
    Позиции с quantity=0 удаляются. Разница по остаткам возвращается на склад.
    Оператор может и убрать позицию, и добавить товар, которого клиент не выбрал
    (например, замену закончившегося) — клиент затем согласует состав целиком.
    """
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    order = _scope_orders(distributor, is_admin).filter(id=order_id).first()
    if not order:
        return JsonResponse({"detail": "Заказ не найден"}, status=404)
    if not order.can_transition_to("adjusted"):
        return JsonResponse({"detail": f"Нельзя корректировать заказ в статусе «{order.get_status_display()}»"}, status=400)

    payload = _json(request)
    updates = payload.get("items") or []
    additions = payload.get("newItems") or []
    reason = (payload.get("reason") or "").strip()
    if not updates and not additions:
        return JsonResponse({"detail": "Передайте позиции для корректировки"}, status=400)

    # Карта itemId -> новое количество
    qty_by_item = {}
    for row in updates:
        try:
            qty_by_item[int(row.get("itemId"))] = max(0, int(row.get("quantity") or 0))
        except (TypeError, ValueError):
            return JsonResponse({"detail": "Некорректные данные позиции"}, status=400)

    # Добавляемые товары: productId -> количество (суммируем дубли в запросе)
    qty_by_new_product = {}
    for row in additions:
        try:
            product_id = int(row.get("productId"))
            quantity = int(row.get("quantity") or 0)
        except (TypeError, ValueError):
            return JsonResponse({"detail": "Некорректные данные новой позиции"}, status=400)
        if quantity > 0:
            qty_by_new_product[product_id] = qty_by_new_product.get(product_id, 0) + quantity

    original_snapshot = _items_snapshot(order)

    with transaction.atomic():
        for item in order.items.select_related("product").all():
            if item.id not in qty_by_item:
                continue
            new_qty = qty_by_item[item.id]
            delta = item.quantity - new_qty  # >0 → вернуть на склад, <0 → списать ещё
            product = item.product

            if delta < 0 and product is not None:
                # оператор увеличил количество — проверяем остаток
                need = -delta
                if product.status != "onOrder" and product.quantity < need:
                    transaction.set_rollback(True)
                    return JsonResponse({
                        "detail": f"Недостаточно «{product.name}» на складе. Доступно: {product.quantity}"
                    }, status=400)

            if product is not None:
                product.quantity = max(0, product.quantity + delta)
                if product.quantity > 5:
                    product.status = "inStock"
                elif product.quantity > 0:
                    product.status = "low"
                elif product.status != "onOrder":
                    product.status = "outOfStock"
                product.save(update_fields=["quantity", "status"])

            if new_qty == 0:
                item.delete()
            elif new_qty != item.quantity:
                item.quantity = new_qty
                item.save(update_fields=["quantity"])

        # Добавление товаров, которых клиент не выбирал
        for product_id, quantity in qty_by_new_product.items():
            product = Product.objects.filter(
                id=product_id, distributor=order.distributor, is_active=True
            ).first()
            if product is None:
                transaction.set_rollback(True)
                return JsonResponse(
                    {"detail": "Товар недоступен у этого дистрибьютора"}, status=400
                )
            if product.status != "onOrder" and product.quantity < quantity:
                transaction.set_rollback(True)
                return JsonResponse({
                    "detail": f"Недостаточно «{product.name}» на складе. Доступно: {product.quantity}"
                }, status=400)

            # Тот же товар уже в заказе — наращиваем позицию, а не плодим дубль.
            existing = order.items.filter(product=product).first()
            if existing:
                existing.quantity += quantity
                existing.save(update_fields=["quantity"])
            else:
                OrderItem.objects.create(
                    order=order,
                    product=product,
                    sku=product.sku,
                    name=product.name,
                    category=product.category,
                    brand=product.brand,
                    volume=product.volume,
                    # Оператор добавляет позицию в чужой заказ — цена всё равно
                    # по рангу клиента, а не базовая из прайса.
                    price=price_for_client(order.client, product),
                    quantity=quantity,
                )

            product.quantity = max(0, product.quantity - quantity)
            if product.quantity > 5:
                product.status = "inStock"
            elif product.quantity > 0:
                product.status = "low"
            elif product.status != "onOrder":
                product.status = "outOfStock"
            product.save(update_fields=["quantity", "status"])

        if not order.items.exists():
            transaction.set_rollback(True)
            return JsonResponse({"detail": "После корректировки в заказе не осталось позиций. Отклоните заказ."}, status=400)

        old_status = order.status
        order.status = "adjusted"
        order.save(update_fields=["status"])

        OrderAdjustment.objects.create(
            order=order,
            original_items=original_snapshot,
            adjusted_items=_items_snapshot(order),
            reason=reason,
            created_by=_current_user(request),
        )

    # Сбрасываем кэш prefetch: сумма и состав в письме и в ответе должны быть
    # уже новыми, а не теми, с которыми заказ загрузился в начале запроса.
    order.refresh_from_db()

    _log_audit(request, f"Order adjust: {old_status} -> adjusted", order, {"reason": reason})
    _notify_client_order(order, "Заказ скорректирован", f"Заказ ORD-{order.id:05d} изменён оператором. Новая сумма: {order.total_amount} ₽. Подтвердите или отмените.")
    _send_order_status_email(order, old_status, "adjusted", recipients=_client_email_list(order))
    return JsonResponse({"order": _format_order(order)})


@csrf_exempt
@require_POST
def reject_order(request, order_id):
    """Оператор отклоняет заказ с указанием причины."""
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    order = _scope_orders(distributor, is_admin).filter(id=order_id).first()
    if not order:
        return JsonResponse({"detail": "Заказ не найден"}, status=404)
    if not order.can_transition_to("rejected"):
        return JsonResponse({"detail": f"Нельзя отклонить заказ в статусе «{order.get_status_display()}»"}, status=400)

    payload = _json(request)
    reason = (payload.get("reason") or "").strip()

    old_status = order.status
    with transaction.atomic():
        _restore_order_stock(order)
        order.status = "rejected"
        order.rejection_reason = reason or "Не указана"
        order.save(update_fields=["status", "rejection_reason"])

    _log_audit(request, f"Order reject: {old_status} -> rejected", order, {"reason": reason})
    _notify_client_order(order, "Заказ отклонён", f"Заказ ORD-{order.id:05d} отклонён. Причина: {order.rejection_reason}")
    _send_order_status_email(order, old_status, "rejected", recipients=_client_email_list(order))
    return JsonResponse({"order": _format_order(order)})


@csrf_exempt
@require_POST
def accept_adjustment(request, order_id):
    """Клиент соглашается со скорректированным заказом: adjusted → confirmed."""
    client, err = _require_client(request)
    if err:
        return err
    order = client.orders.filter(id=order_id).first()
    if not order:
        return JsonResponse({"detail": "Заказ не найден"}, status=404)
    if order.status != "adjusted":
        return JsonResponse({"detail": "Заказ не в статусе корректировки"}, status=400)

    old_status = order.status
    order.status = "confirmed"
    order.confirmed_at = timezone.now()
    order.save(update_fields=["status", "confirmed_at"])

    _notify_operator_order(order, "Клиент принял корректировку", f"{order.client.company_name} принял корректировку заказа ORD-{order.id:05d}")
    _send_order_status_email(order, old_status, "confirmed")
    return JsonResponse({"order": _format_order(order)})


@csrf_exempt
@require_POST
def ship_order(request, order_id):
    """Оператор отмечает отправку: paid → shipped."""
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    order = _scope_orders(distributor, is_admin).filter(id=order_id).first()
    if not order:
        return JsonResponse({"detail": "Заказ не найден"}, status=404)
    if not order.can_transition_to("shipped"):
        return JsonResponse({"detail": f"Нельзя отправить заказ в статусе «{order.get_status_display()}»"}, status=400)

    old_status = order.status
    order.status = "shipped"
    order.shipped_at = timezone.now()
    order.save(update_fields=["status", "shipped_at"])

    _log_audit(request, f"Order ship: {old_status} -> shipped", order)
    _notify_client_order(order, "Заказ отправлен", f"Заказ ORD-{order.id:05d} отправлен. Ожидайте доставку.")
    _send_order_status_email(order, old_status, "shipped", recipients=_client_email_list(order))
    return JsonResponse({"order": _format_order(order)})


# ── Оплата через YooKassa ──────────────────────────────────────────────────────

def _format_payment(payment):
    return {
        "id": str(payment.id),
        "orderId": str(payment.order_id),
        "provider": payment.provider,
        "providerPaymentId": payment.provider_payment_id,
        "amount": float(payment.amount),
        "currency": payment.currency,
        "status": payment.status,
        "confirmationUrl": payment.confirmation_url or None,
        "createdAt": payment.created_at.isoformat(),
        "paidAt": payment.paid_at.isoformat() if payment.paid_at else None,
    }


def _mark_order_paid(order, payment):
    """Перевести заказ в «оплачен» и разослать всё, что с этим связано.

    Путей оплаты теперь два — ЮKassa и полное покрытие бонусами, — а следствия
    у них одинаковые, поэтому они живут здесь, а не в каждом обработчике.
    """
    if order.status not in Order.PAYABLE_STATUSES:
        return  # уже оплачен: повторно ничего не делаем

    old_status = order.status
    order.status = "paid"
    order.paid_at = timezone.now()
    order.save(update_fields=["status", "paid_at"])

    _notify_operator_order(
        order,
        "Заказ оплачен",
        f"{order.client.company_name} оплатил заказ ORD-{order.id:05d} на {payment.amount} ₽",
    )
    _notify_client_order(
        order, "Оплата получена", f"Оплата заказа ORD-{order.id:05d} прошла успешно."
    )
    _send_order_status_email(order, old_status, "paid")
    # Оплаченный заказ — часть оборота: клиент мог дорасти до ранга с большей
    # скидкой.
    sync_client_tier(order.client)


@csrf_exempt
@require_POST
def pay_order(request, order_id):
    """Клиент инициирует оплату подтверждённого заказа через YooKassa.

    Оплатить можно ТОЛЬКО заказ в статусе confirmed/adjusted (гарантия наличия
    товара). Возвращает confirmation_url, куда нужно перенаправить клиента.
    Статус заказа станет `paid` только после webhook `payment.succeeded`.
    """
    from api.services import payments as pay

    client, err = _require_client(request)
    if err:
        return err
    order = client.orders.filter(id=order_id).first()
    if not order:
        return JsonResponse({"detail": "Заказ не найден"}, status=404)
    if order.status not in Order.PAYABLE_STATUSES:
        return JsonResponse(
            {"detail": "Оплатить можно только подтверждённый заказ"}, status=400
        )
    if order.total_amount <= 0:
        return JsonResponse({"detail": "Сумма заказа равна нулю"}, status=400)

    # Переиспользуем ещё не оплаченный платёж (не создаём дубли при повторном тапе)
    existing = order.payments.filter(status__in=["pending", "waiting_for_capture"]).first()
    if existing and existing.confirmation_url:
        return JsonResponse({"payment": _format_payment(existing)}, status=200)

    # Бонусы уменьшают сумму, которая уходит в ЮKassa. Списываем до обращения
    # к провайдеру: платить нужно уже остаток.
    from api.services import bonuses

    payload = _json(request)
    raw_bonus = payload.get("useBonus")
    if raw_bonus is True:
        # true — «списать сколько можно», чтобы приложению не считать самому.
        requested_bonus = bonuses.spendable_for_order(client, order.total_amount)
    else:
        try:
            requested_bonus = Decimal(str(raw_bonus or 0))
        except (InvalidOperation, ValueError):
            return JsonResponse({"detail": "Некорректная сумма бонусов"}, status=400)
    if requested_bonus < 0:
        return JsonResponse({"detail": "Сумма бонусов не может быть отрицательной"}, status=400)

    applied_bonus = bonuses.debit_for_order(client, order, requested_bonus)
    payable = (Decimal(order.total_amount) - applied_bonus).quantize(Decimal("0.01"))

    # Бонус покрыл заказ целиком — платить нечего, в ЮKassa идти незачем.
    if payable <= 0:
        payment = Payment.objects.create(
            order=order,
            amount=Decimal("0.00"),
            currency="RUB",
            status="succeeded",
            provider="bonus",
            paid_at=timezone.now(),
        )
        _mark_order_paid(order, payment)
        return JsonResponse(
            {"payment": _format_payment(payment), "bonusApplied": float(applied_bonus)},
            status=201,
        )

    if not pay.is_configured():
        # Бонус списан, а заплатить остаток нечем — возвращаем на счёт.
        bonuses.refund_for_order(order)
        return JsonResponse(
            {"detail": "Оплата временно недоступна: не настроены реквизиты YooKassa (YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY)."},
            status=503,
        )

    idempotence_key = pay.new_idempotence_key()
    payment = Payment.objects.create(
        order=order,
        amount=payable,
        currency="RUB",
        status="pending",
        idempotence_key=idempotence_key,
    )
    try:
        resp = pay.create_payment(
            order=order,
            amount=payable,
            idempotence_key=idempotence_key,
            description=f"Заказ ORD-{order.id:05d} · {order.client.company_name}",
        )
    except pay.PaymentProviderError as exc:
        payment.status = "canceled"
        payment.raw_response = {"error": str(exc)}
        payment.save(update_fields=["status", "raw_response"])
        # Платёж не создан — бонус не должен сгореть.
        bonuses.refund_for_order(order)
        return JsonResponse({"detail": f"Ошибка платёжного провайдера: {exc}"}, status=502)

    payment.provider_payment_id = resp.get("id", "")
    payment.status = resp.get("status", "pending")
    payment.confirmation_url = (resp.get("confirmation") or {}).get("confirmation_url", "")
    payment.raw_response = resp
    payment.save(update_fields=["provider_payment_id", "status", "confirmation_url", "raw_response"])

    return JsonResponse(
        {"payment": _format_payment(payment), "bonusApplied": float(applied_bonus)},
        status=201,
    )


@csrf_exempt
@require_POST
def yookassa_webhook(request):
    """Приём уведомлений от YooKassa (payment.succeeded / payment.canceled).

    Настраивается в ЛК ЮKassa: HTTP-уведомления → указать URL этого endpoint-а.
    Мы перепроверяем статус платежа через API, чтобы не доверять телу запроса.
    """
    from api.services import payments as pay

    payload = _json(request)
    event = payload.get("event")
    obj = payload.get("object") or {}
    provider_payment_id = obj.get("id")
    if not provider_payment_id:
        return JsonResponse({"detail": "no payment id"}, status=400)

    payment = Payment.objects.filter(provider_payment_id=provider_payment_id).select_related("order").first()
    if not payment:
        # Платёж не наш / уже удалён — отвечаем 200, чтобы ЮKassa не ретраила бесконечно
        return JsonResponse({"detail": "unknown payment"}, status=200)

    # Перепроверяем реальный статус у провайдера (защита от подделки webhook-а)
    real_status = obj.get("status")
    if pay.is_configured():
        try:
            fresh = pay.fetch_payment(provider_payment_id)
            real_status = fresh.get("status", real_status)
        except pay.PaymentProviderError:
            logger.exception("YooKassa verify failed for %s", provider_payment_id)

    order = payment.order

    if event == "payment.succeeded" or real_status == "succeeded":
        if payment.status != "succeeded":
            payment.status = "succeeded"
            payment.paid_at = timezone.now()
            payment.save(update_fields=["status", "paid_at"])
        # Двигаем заказ в paid только из оплачиваемого статуса (идемпотентно)
        _mark_order_paid(order, payment)
    elif event == "payment.canceled" or real_status == "canceled":
        payment.status = "canceled"
        payment.save(update_fields=["status"])
        # Оплата не состоялась — списанные бонусы возвращаем на счёт.
        from api.services import bonuses

        bonuses.refund_for_order(order)

    return JsonResponse({"status": "ok"})


@require_GET
def purchases(request):
    client, err = _require_client(request)
    if err:
        return err
    qs = client.purchases.prefetch_related("items").order_by("-date")

    # Сводка считается по всей выборке, а не по текущей странице — иначе
    # итоги на экране менялись бы по мере подгрузки.
    pending_statuses = ["new", "pending", "pending_verification", "under_review", "duplicate_review"]
    stats = {
        "totalAmount": float(
            qs.exclude(status="rejected").aggregate(total=Sum("total_amount"))["total"] or 0
        ),
        "verifiedCount": qs.filter(status="verified").count(),
        "pendingCount": qs.filter(status__in=pending_statuses).count(),
    }
    return JsonResponse(paginated_response(request, qs, _format_purchase, extra={"stats": stats}))


@csrf_exempt
@require_POST
@csrf_exempt
@require_POST
def create_purchase(request):
    client, err = _require_client(request)
    if err:
        return err
    
    is_multipart = (request.content_type or "").startswith("multipart/form-data")
    payload = request.POST if is_multipart else _json(request)
    files = _attachment_files(request)
    
    serializer = PurchaseSerializer(payload)
    if not serializer.is_valid():
        return JsonResponse({"errors": serializer.errors}, status=400)
    
    validated_data = serializer.validated_data
    
    existing = Purchase.objects.filter(
        client=client,
        document_number=validated_data['document_number'],
        date=validated_data['date'],
        total_amount=validated_data['amount']
    ).first()
    
    if existing:
        return JsonResponse({
            "error": "duplicate_detected",
            "message": f"Покупка с такими данными уже загружена и имеет статус [{existing.get_status_display()}]"
        }, status=400)
    
    with transaction.atomic():
        purchase = Purchase.objects.create(
            client=client,
            distributor=client.distributor,
            document_number=validated_data['document_number'],
            date=validated_data['date'],
            total_amount=validated_data['amount'],
            status="new",
        )
        
        items_data = payload.get("items")
        if isinstance(items_data, str):
            try:
                items_data = json.loads(items_data)
            except json.JSONDecodeError:
                items_data = []
        
        items = _parse_items(items_data)
        for raw in items:
            PurchaseItem.objects.create(
                purchase=purchase, 
                sku=raw.get("sku"), 
                name=raw.get("name"), 
                quantity=int(raw.get("quantity") or 1), 
                price=_money_value(raw.get("price"))
            )
        
        _, attach_error = _create_attachments(request, purchase, files, description="Документ к покупке")
        if attach_error:
            # Don't keep a purchase whose proof-of-payment failed to upload.
            transaction.set_rollback(True)
            return attach_error

    _notify(
        getattr(client.distributor, "user", None),
        "Новая покупка на проверку",
        f"{client.company_name}: покупка {purchase.document_number} на {purchase.total_amount} ₽",
        "action_required",
        link="/distributor",
    )
    return JsonResponse({"purchase": _format_purchase(purchase)}, status=201)


# Color Lab Views

@require_GET
def color_requests(request):
    client, err = _require_client(request)
    if err:
        return err
    qs = client.color_requests.prefetch_related("materials", "courier_tasks").all()
    if request.GET.get("active") == "true":
        qs = qs.exclude(status__in=["delivered", "cancelled"])
    return JsonResponse(paginated_response(request, qs, _format_color_request))


@csrf_exempt
@require_POST
def cancel_color_request(request, request_id):
    client, err = _require_client(request)
    if err:
        return err
    
    color_request = client.color_requests.filter(id=request_id).first()
    if not color_request:
        return JsonResponse({"detail": "Заявка не найдена"}, status=404)
        
    if color_request.status != "created":
        return JsonResponse({"detail": "Нельзя отменить заявку, которая уже в работе"}, status=400)
        
    color_request.status = "cancelled"
    color_request.save(update_fields=["status"])
    
    return JsonResponse({"status": "success"})


@csrf_exempt
@require_POST
def update_color_request(request, request_id):
    client, err = _require_client(request)
    if err:
        return err
    
    color_request = client.color_requests.filter(id=request_id).first()
    if not color_request:
        return JsonResponse({"detail": "Заявка не найдена"}, status=404)
        
    if color_request.status != "created":
        return JsonResponse({"detail": "Нельзя изменить заявку, которая уже в работе"}, status=400)
        
    payload = _json(request)
    color_request.car_brand = payload.get("carBrand", color_request.car_brand)
    color_request.car_model = payload.get("carModel", color_request.car_model)
    color_request.car_year = (payload.get("carYear", color_request.car_year) or "").strip()[:4]
    color_request.vin = payload.get("vin", color_request.vin)
    color_request.color_code = payload.get("colorCode", color_request.color_code)
    color_request.color_name = payload.get("colorName", color_request.color_name)
    if "paintTypeNote" in payload:
        color_request.paint_type_note = (payload.get("paintTypeNote") or "").strip()[:255]
    if "paintType" in payload:
        paint_type, err = _paint_type(payload.get("paintType"))
        if err:
            return err
        color_request.paint_type = paint_type
    color_request.pickup_address = payload.get("pickupAddress", color_request.pickup_address)
    color_request.contact_person = payload.get("contactPerson", color_request.contact_person)
    color_request.contact_phone = payload.get("contactPhone", color_request.contact_phone)
    color_request.comment = payload.get("comment", color_request.comment)
    color_request.transfer_method = payload.get("transferMethod", color_request.transfer_method)
    if "urgent" in payload:
        color_request.urgent = _bool(payload.get("urgent"))
    if "pickupTime" in payload:
        color_request.pickup_time = _dt(payload.get("pickupTime"))
    if "courierArriveUntil" in payload:
        color_request.courier_arrive_until = _time(payload.get("courierArriveUntil"))

    color_request.save()
    return JsonResponse({"request": _format_color_request(color_request)})


def _paint_type(raw):
    """Тип покрытия из запроса. Пустой или незнакомый — ошибка: от типа зависит
    цена, молча подставлять умолчание нельзя."""
    value = (raw or "").strip()
    allowed = [choice for choice, _ in ColorRequest.PAINT_TYPE_CHOICES]
    if value not in allowed:
        return None, JsonResponse(
            {"detail": "Укажите тип покрытия: " + ", ".join(allowed)}, status=400
        )
    return value, None


def _append_color_history(item, status, user=None, comment=""):
    history_item = {
        "status": status,
        "at": timezone.now().isoformat(),
        "by": str(user.id) if user else None,
        "comment": comment,
    }
    item.status_history = [*(item.status_history or []), history_item]


@csrf_exempt
@require_POST
def create_color_request(request):
    client, err = _require_client(request)
    if err:
        return err
    payload = request.POST if (request.content_type or "").startswith("multipart/form-data") else _json(request)
    files = _attachment_files(request)
    paint_type, err = _paint_type(payload.get("paintType"))
    if err:
        return err
    try:
        with transaction.atomic():
            urgent = _bool(payload.get("urgent"))
            item = ColorRequest.objects.create(
                client=client,
                car_brand=(payload.get("carBrand") or "").strip(),
                car_model=(payload.get("carModel") or "").strip(),
                car_year=(payload.get("carYear") or "").strip()[:4],
                vin=(payload.get("vin") or "").strip(),
                color_code=(payload.get("colorCode") or "").strip(),
                color_name=(payload.get("colorName") or "").strip(),
                paint_type=paint_type,
                paint_type_note=(payload.get("paintTypeNote") or "").strip()[:255],
                urgent=urgent,
                comment=(payload.get("comment") or "").strip(),
                transfer_method=(payload.get("transferMethod") or "courier").strip(),
                pickup_address=(payload.get("pickupAddress") or payload.get("address") or client.city).strip(),
                pickup_time=_dt(payload.get("pickupTime") or payload.get("pickupDate") or payload.get("scheduledTime")),
                courier_arrive_until=_time(payload.get("courierArriveUntil")),
                contact_person=(payload.get("contactPerson") or payload.get("contactName") or client.contact_name).strip(),
                contact_phone=(payload.get("contactPhone") or client.phone).strip(),
                assigned_distributor=client.distributor,
            )
            sla_hours = 4 if item.urgent else 24
            item.sla_deadline = timezone.now() + timezone.timedelta(hours=sla_hours)
            _append_color_history(item, "created", _current_user(request), "Заявка создана")
            item.save(update_fields=["sla_deadline", "status_history"])
            _create_attachments(request, item, files, description="Фото для Color Lab")
        _notify(
            getattr(client.distributor, "user", None),
            "Новая заявка на подбор цвета",
            f"{client.company_name}: {item.car_brand} {item.car_model}, код {item.color_code}",
            "color",
            link="/distributor/color-lab",
        )
        return JsonResponse({"request": _format_color_request(item)}, status=201)
    except Exception as e:
        return JsonResponse({"detail": f"Ошибка создания заявки: {str(e)}"}, status=400)


# Courier Views

@require_GET
def courier_tasks(request):
    client, err = _require_client(request)
    if err:
        return err
    # Карточка доставки показывает курьера и заказ — тянем их одним запросом.
    qs = client.courier_tasks.select_related("courier", "order", "client")
    if request.GET.get("active") == "true":
        qs = qs.exclude(status__in=["delivered", "returned", "cancelled"])
    return JsonResponse(paginated_response(request, qs, _format_courier_task))


@csrf_exempt
@require_POST
def create_courier_task(request):
    client, err = _require_client(request)
    if err:
        return err
    payload = _json(request)
    task = CourierTask.objects.create(
        client=client,
        task_type=payload.get("type", "delivery"),
        address=payload.get("address", client.city),
        time_slot=payload.get("timeSlot", "10:00 - 18:00"),
        scheduled_time=_dt(payload.get("scheduledTime")),
        contact_name=payload.get("contactName", client.contact_name),
        contact_phone=payload.get("contactPhone", client.phone),
        car_description=payload.get("carDescription", ""),
        comment=payload.get("comment", ""),
    )
    _append_task_history(task, "created", _current_user(request), "Создано клиентом")
    task.save(update_fields=["status_history"])
    _notify(
        getattr(client.distributor, "user", None),
        "Новая заявка на доставку",
        f"{client.company_name}: {task.get_task_type_display()} · {task.address}",
        "delivery",
        link="/distributor",
    )
    return JsonResponse({"task": _format_courier_task(task)}, status=201)


@csrf_exempt
@require_POST
def cancel_courier_task(request, task_id):
    client, err = _require_client(request)
    if err:
        return err
    
    task = client.courier_tasks.filter(id=task_id).first()
    if not task:
        return JsonResponse({"detail": "Заявка не найдена"}, status=404)
        
    if task.status != "created":
        return JsonResponse({"detail": "Нельзя отменить заявку, которая уже в работе"}, status=400)
        
    task.status = "cancelled"
    task.save(update_fields=["status"])
    _append_task_history(task, "cancelled", client.user, "Отменено клиентом")
    
    return JsonResponse({"status": "success"})


@csrf_exempt
@require_POST
def update_courier_task(request, task_id):
    client, err = _require_client(request)
    if err:
        return err
    
    task = client.courier_tasks.filter(id=task_id).first()
    if not task:
        return JsonResponse({"detail": "Заявка не найдена"}, status=404)
        
    if task.status != "created":
        return JsonResponse({"detail": "Нельзя изменить заявку, которая уже в работе"}, status=400)
        
    payload = _json(request)
    task.task_type = payload.get("type", task.task_type)
    task.address = payload.get("address", task.address)
    task.contact_name = payload.get("contactName", task.contact_name)
    task.contact_phone = payload.get("contactPhone", task.contact_phone)
    task.time_slot = payload.get("timeSlot", task.time_slot)
    task.comment = payload.get("comment", task.comment)
    
    task.save()
    return JsonResponse({"task": _format_courier_task(task)})


@csrf_exempt
@require_POST
def courier_task_proof(request, task_id):
    client, err = _require_client(request)
    if err:
        return err
    task = client.courier_tasks.filter(id=task_id).first()
    if task:
        files = _attachment_files(request)
        _create_attachments(request, task, files, description="Фото подтверждение")
    return JsonResponse({"task": _format_courier_task(task)})


@require_GET
def courier_my_tasks(request):
    user, is_admin, err = _require_courier_scope(request)
    if err:
        return err
    
    # Задачи, назначенные на текущего курьера
    qs = CourierTask.objects.filter(courier=user).order_by("-created_at")
    if is_admin:
        qs = CourierTask.objects.all().order_by("-created_at")

    status_filter = request.GET.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter)

    return JsonResponse(paginated_response(request, qs, _format_courier_task))

@csrf_exempt
@require_http_methods(["PATCH", "POST"])
def courier_update_task_status(request, task_id):
    user, is_admin, err = _require_courier_scope(request)
    if err:
        return err
    
    task = CourierTask.objects.filter(id=task_id).first()
    if not task:
        return JsonResponse({"detail": "Задача не найдена"}, status=404)
    
    if not is_admin and task.courier != user:
        return JsonResponse({"detail": "Нет прав для редактирования этой задачи"}, status=403)

    # Приоритетно берем из POST (для multipart), затем из JSON
    status = request.POST.get("status")
    comment = request.POST.get("courier_comment")

    if not status:
        payload = _json(request)
        status = payload.get("status")
        if not comment:
            comment = payload.get("courier_comment")

    if status:
        task.status = status
        _append_task_history(task, status, user)
    
    if comment is not None:
        task.courier_comment = comment

    # Обработка фото-подтверждения
    if "proof_photo" in request.FILES:
        task.proof_photo = request.FILES["proof_photo"]

    task.save()
    return JsonResponse({"task": _format_courier_task(task)})


@csrf_exempt
@require_POST
def assign_courier_task(request, task_id):
    user, is_admin, err = _require_courier_scope(request)
    if not is_admin:
        return JsonResponse({"detail": "Только администратор может назначать курьеров"}, status=403)
    courier_id = _json(request).get("courierId")
    task = CourierTask.objects.filter(id=task_id).first()
    if task:
        task.courier_id = courier_id
        task.status = "assigned"
        _append_task_history(task, "assigned", user, f"Назначен курьер {courier_id}")
        task.save(update_fields=["courier", "status", "status_history"])
    return JsonResponse({"task": _format_courier_task(task)})


# Distributor Views

@require_GET
def distributors(request):
    user = _current_user(request)
    if not user:
        return JsonResponse({"detail": "Unauthorized"}, status=401)
    
    profile = getattr(user, "profile", None)
    is_global = _is_user_global(user, profile)
    
    qs = Distributor.objects.all()
    if not is_global and profile and profile.role == "manager":
        qs = qs.filter(managed_regions__manager=user)
        
    return JsonResponse(paginated_response(
        request,
        qs.distinct(),
        lambda d: {"id": str(d.id), "name": d.name},
    ))


def _is_user_global(user, profile=None):
    if user.is_superuser:
        return True
    if profile is None:
        profile = getattr(user, "profile", None)
    return profile and profile.role == "admin"


def _require_manager_scope(request):
    user = _current_user(request)
    if user is None:
        return None, False, JsonResponse({"detail": "Unauthorized"}, status=401)
    
    profile = getattr(user, "profile", None)
    is_global = _is_user_global(user, profile)
    
    if profile and profile.role in ["manager", "admin"] or user.is_staff or user.is_superuser:
        return user, is_global, None
        
    return None, False, JsonResponse({"detail": "Нет доступа менеджера"}, status=403)


def _get_manager_regions(user, is_global):
    if is_global:
        return None  # All regions
    return user.managed_regions.all()


def _filter_by_manager_scope(user, is_global, qs, region_path="region"):
    regions = _get_manager_regions(user, is_global)
    if regions is None:
        return qs
    return qs.filter(**{f"{region_path}__in": regions})


@require_GET
def export_clients_file(request):
    """Выгрузка списка клиентов из приложения: Excel, Word, PDF или CSV.

    Кто что видит:
      • дистрибьютор — клиентов своих регионов;
      • менеджер региона — клиентов закреплённых за ним регионов;
      • главный менеджер и админ — всех.

    Фильтры и поиск те же, что в списках, поэтому выгрузить можно ровно то,
    что человек видит на экране.
    """
    user = _current_user(request)
    if user is None:
        return JsonResponse({"detail": "Unauthorized"}, status=401)

    fmt = (request.GET.get("format") or "xlsx").strip().lower()
    if fmt not in EXPORT_FORMATS:
        return JsonResponse(
            {"detail": f"Неизвестный формат: {fmt}. Доступны: {', '.join(EXPORT_FORMATS)}"},
            status=400,
        )

    qs, err = _clients_for_export(request, user)
    if err:
        return err

    search = (request.GET.get("search") or "").strip()
    if search:
        qs = qs.filter(Q(company_name__icontains=search) | Q(inn__icontains=search))
    for param, field in (("status", "status"), ("region", "region_id"), ("partnerStatus", "partner_status")):
        value = (request.GET.get(param) or "").strip()
        if value:
            qs = qs.filter(**{field: value})
    category = (request.GET.get("category") or "").strip()
    if category:
        qs = qs.filter(category=category.lower())

    try:
        return export_clients(qs.order_by("company_name"), fmt)
    except ExportUnavailable as error:
        # Формат не собрать — на сервере нет библиотеки. Отдаём понятный текст
        # с командой установки: 500 без объяснений тут бесполезен.
        logger.warning("Экспорт %s недоступен: %s", fmt, error)
        return JsonResponse({"detail": str(error)}, status=503)


def _clients_for_export(request, user):
    """Выборка клиентов по роли. Возвращает (queryset, error_response)."""
    profile = getattr(user, "profile", None)
    role = getattr(profile, "role", None)

    if role == "distributor":
        distributor, is_admin, err = _require_distributor_scope(request)
        if err:
            return None, err
        return _scope_clients(distributor, is_admin), None

    if role in ("manager", "admin") or user.is_staff or user.is_superuser:
        manager, is_global, err = _require_manager_scope(request)
        if err:
            return None, err
        qs = ClientProfile.objects.select_related("user", "region", "distributor", "manager")
        return _filter_by_manager_scope(manager, is_global, qs), None

    return None, JsonResponse({"detail": "Нет доступа к выгрузке клиентов"}, status=403)


def _format_manager_client(client):
    base = _format_client(client)
    base['distributorName'] = client.distributor.name if client.distributor else None
    base['regionId'] = str(client.region_id) if client.region_id else None
    return base


def _format_manager_task(task):
    return {
        'id': str(task.id),
        'clientId': str(task.client_id) if task.client_id else None,
        'clientName': task.client.company_name if task.client_id else '',
        'managerId': str(task.manager_id),
        'managerName': task.manager.get_full_name() or task.manager.username,
        'text': task.text,
        'deadline': task.deadline.isoformat() if task.deadline else None,
        'status': task.status,
        'comment': task.comment,
        'createdAt': task.created_at.isoformat(),
    }


def _format_contact_history(entry):
    return {
        'id': str(entry.id),
        'clientId': str(entry.client_id),
        'type': entry.contact_type,
        'typeDisplay': entry.get_contact_type_display(),
        'result': entry.result,
        'date': entry.date.isoformat(),
        'authorId': str(entry.manager_id),
        'authorName': entry.manager.get_full_name() or entry.manager.username,
    }


@require_GET
def manager_dashboard(request):
    user, is_global, err = _require_manager_scope(request)
    if err:
        return err
    
    # Filters
    region_id = request.GET.get("region")
    distributor_id = request.GET.get("distributor")
    
    client_qs = _filter_by_manager_scope(user, is_global, ClientProfile.objects.all()).distinct()
    order_qs = _filter_by_manager_scope(user, is_global, Order.objects.all(), "client__region").distinct()
    
    if region_id:
        client_qs = client_qs.filter(region_id=region_id)
        order_qs = order_qs.filter(client__region_id=region_id)
    if distributor_id:
        client_qs = client_qs.filter(distributor_id=distributor_id)
        order_qs = order_qs.filter(distributor_id=distributor_id)

    # Scoped regions for regionalStats
    managed_regions = _get_manager_regions(user, is_global)
    if managed_regions is None:
        regions_for_stats = Region.objects.all()
    else:
        regions_for_stats = managed_regions

    stats = {
        "totalClients": client_qs.count(),
        "activeOrders": order_qs.filter(status__in=["new", "accepted"]).count(),
        "fulfilledOrders": order_qs.filter(status="fulfilled").count(),
        "totalTurnover": sum((o.total_amount for o in order_qs.filter(status="fulfilled").prefetch_related("items")), start=0),
        "regionalStats": [
            {
                "region": r.name,
                "clients": client_qs.filter(region=r).count(),
                "orders": order_qs.filter(client__region=r).count()
            } for r in regions_for_stats
        ]
    }
    
    return JsonResponse(stats)


@csrf_exempt
@require_POST
def erp_stock_update(request):
    token_str = request.headers.get("X-Integration-Token")
    if not token_str:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token_str = auth_header.split(" ")[1]
            
    if not token_str:
        return JsonResponse({"detail": "Token is missing"}, status=401)
        
    token = IntegrationToken.objects.filter(token=token_str, is_active=True).select_related("distributor").first()
    if not token:
        return JsonResponse({"detail": "Invalid or inactive token"}, status=401)
        
    distributor = token.distributor
    payload = _json(request)
    dry_run = request.GET.get("dry_run") == "true"
    
    if not isinstance(payload, list):
        return JsonResponse({"detail": "Expected a JSON array"}, status=400)
        
    updated_count = 0
    errors = []
    results = []
    
    try:
        for item in payload:
            sku = item.get("sku")
            external_id = item.get("external_id")
            quantity = item.get("quantity")
            price = item.get("price")
            
            if not (sku or external_id) or quantity is None:
                err = f"Missing (sku or external_id) or quantity in item: {item}"
                errors.append(err)
                results.append({"item": item, "status": "error", "message": err})
                continue
                
            try:
                product = None
                if external_id:
                    product = Product.objects.filter(distributor=distributor, external_id=external_id).first()
                
                if not product and sku:
                    product = Product.objects.filter(distributor=distributor, sku=sku).first()

                if product:
                    if not dry_run:
                        product.quantity = int(quantity)
                        if price is not None:
                            product.price = Decimal(str(price))
                        
                        if product.quantity > 5:
                            product.status = "inStock"
                        elif product.quantity > 0:
                            product.status = "low"
                        else:
                            product.status = "outOfStock"
                        product.save()
                    
                    updated_count += 1
                    results.append({"sku": sku, "external_id": external_id, "status": "updated" if not dry_run else "valid"})
                else:
                    err = f"Product not found (sku: {sku}, external_id: {external_id})"
                    errors.append(err)
                    results.append({"sku": sku, "external_id": external_id, "status": "error", "message": err})
                    
            except Exception as e:
                err = f"Error processing '{sku or external_id}': {str(e)}"
                errors.append(err)
                results.append({"sku": sku, "external_id": external_id, "status": "error", "message": err})
                
        status = "success" if not errors else ("partial" if updated_count > 0 else "error")
        if dry_run:
            status = f"dry_run_{status}"

        details = {
            "updated_count": updated_count,
            "error_count": len(errors),
            "dry_run": dry_run,
            "results": results[:100], # Limit log size
        }
        
        SyncLog.objects.create(
            distributor=distributor,
            sync_type="stock_update",
            status="success" if not errors else "error",
            details=details
        )
        
        return JsonResponse({
            "status": status,
            "updated": updated_count,
            "errors": errors,
            "dry_run": dry_run,
            "total_items": len(payload)
        })
    except Exception as e:
        SyncLog.objects.create(
            distributor=distributor,
            sync_type="stock_update",
            status="error",
            details={"error": str(e)}
        )
        return JsonResponse({"detail": "Internal server error"}, status=500)


@csrf_exempt
@require_POST
def erp_catalog_sync(request):
    """
    1C pushes full or partial product catalog.
    Allows creating new products and updating existing ones by external_id or SKU.
    """
    token_str = request.headers.get("X-Integration-Token")
    if not token_str:
        return JsonResponse({"detail": "Token is missing"}, status=401)
    token = IntegrationToken.objects.filter(token=token_str, is_active=True).select_related("distributor").first()
    if not token:
        return JsonResponse({"detail": "Invalid token"}, status=401)
    
    distributor = token.distributor
    payload = _json(request)
    if not isinstance(payload, list):
        return JsonResponse({"detail": "Expected a JSON array"}, status=400)
    
    created_count = 0
    updated_count = 0
    errors = []
    
    for item in payload:
        external_id = item.get("external_id")
        sku = item.get("sku")
        if not (external_id or sku):
            errors.append(f"Missing both external_id and sku in item: {item}")
            continue
            
        try:
            # Try to find existing product
            product = None
            if external_id:
                product = Product.objects.filter(distributor=distributor, external_id=external_id).first()
            if not product and sku:
                product = Product.objects.filter(distributor=distributor, sku=sku).first()
            
            defaults = {
                "name": item.get("name", "Новый товар"),
                "category": item.get("category", "Без категории"),
                "brand": item.get("brand", "AutoTerra"),
                "price": Decimal(str(item.get("price", 0))),
                "quantity": int(item.get("quantity", 0)),
                "is_active": True,
            }
            if external_id: defaults["external_id"] = external_id
            if sku: defaults["sku"] = sku

            if product:
                for key, value in defaults.items():
                    setattr(product, key, value)
                product.save()
                updated_count += 1
            else:
                Product.objects.create(distributor=distributor, **defaults)
                created_count += 1
                
        except Exception as e:
            errors.append(f"Error processing {sku or external_id}: {str(e)}")

    SyncLog.objects.create(
        distributor=distributor,
        sync_type="catalog_sync",
        status="success" if not errors else "partial",
        details={"created": created_count, "updated": updated_count, "errors": errors}
    )
    
    return JsonResponse({
        "status": "success" if not errors else "partial",
        "created": created_count,
        "updated": updated_count,
        "errors": errors
    })


@csrf_exempt
@require_POST
def erp_client_sync(request):
    """
    1C pushes client data to map external_id by INN or update existing profiles.
    """
    token_str = request.headers.get("X-Integration-Token")
    if not token_str:
        return JsonResponse({"detail": "Token is missing"}, status=401)
    token = IntegrationToken.objects.filter(token=token_str, is_active=True).select_related("distributor").first()
    if not token:
        return JsonResponse({"detail": "Invalid token"}, status=401)
    
    distributor = token.distributor
    payload = _json(request)
    
    updated_count = 0
    errors = []
    
    for item in payload:
        inn = item.get("inn")
        external_id = item.get("external_id")
        if not (inn or external_id):
            errors.append("Missing inn or external_id")
            continue
            
        try:
            client = None
            if external_id:
                client = ClientProfile.objects.filter(distributor=distributor, external_id=external_id).first()
            if not client and inn:
                client = ClientProfile.objects.filter(distributor=distributor, inn=inn).first()
            
            if client:
                client.external_id = external_id or client.external_id
                if item.get("partner_status"):
                    client.partner_status = item.get("partner_status")
                client.save()
                updated_count += 1
            else:
                errors.append(f"Client not found for mapping: {inn or external_id}")
        except Exception as e:
            errors.append(f"Error mapping {inn}: {str(e)}")
            
    return JsonResponse({"updated": updated_count, "errors": errors})


@require_GET
def erp_orders_export(request):
    """
    1C polls for new orders that haven't been exported yet.
    """
    token_str = request.headers.get("X-Integration-Token")
    if not token_str:
        return JsonResponse({"detail": "Token is missing"}, status=401)
    token = IntegrationToken.objects.filter(token=token_str, is_active=True).select_related("distributor").first()
    if not token:
        return JsonResponse({"detail": "Invalid token"}, status=401)
    
    orders = Order.objects.filter(distributor=token.distributor, external_id__isnull=True).prefetch_related("items")
    
    results = []
    for o in orders:
        results.append({
            "id": o.id,
            "client_inn": o.client.inn,
            "client_external_id": o.client.external_id,
            "created_at": o.created_at.isoformat(),
            "comment": o.comment,
            "items": [
                {"sku": i.sku, "name": i.name, "quantity": i.quantity, "price": str(i.price)}
                for i in o.items.all()
            ]
        })
    
    return JsonResponse({"orders": results})


import xml.etree.ElementTree as ET
from django.conf import settings
import os

@csrf_exempt
def erp_1c_exchange(request):
    """
    Standard CommerceML (Bitrix-compatible) exchange protocol.
    Enables 'Exchange with website' in 1C without coding.
    """
    mode = request.GET.get("mode")
    type_ = request.GET.get("type")
    token_str = request.GET.get("token") or request.headers.get("X-Integration-Token")

    if not token_str:
        return HttpResponse("failure\nToken missing", content_type="text/plain", status=401)

    token = IntegrationToken.objects.filter(token=token_str, is_active=True).select_related("distributor").first()
    if not token:
        return HttpResponse("failure\nInvalid token", content_type="text/plain", status=401)

    distributor = token.distributor

    if mode == "checkauth":
        return HttpResponse(f"success\nautoterra_sid\n{token_str}", content_type="text/plain")

    if mode == "init":
        return HttpResponse("zip=no\nfile_limit=10000000", content_type="text/plain")

    if mode == "file":
        filename = request.GET.get("filename")
        if not filename:
            return HttpResponse("failure\nNo filename", content_type="text/plain")

        temp_dir = os.path.join(settings.MEDIA_ROOT, "temp_1c", str(distributor.id))
        os.makedirs(temp_dir, exist_ok=True)

        filepath = os.path.join(temp_dir, filename)
        with open(filepath, "wb") as f:
            f.write(request.body)

        return HttpResponse("success", content_type="text/plain")

    if mode == "import":
        filename = request.GET.get("filename")
        temp_dir = os.path.join(settings.MEDIA_ROOT, "temp_1c", str(distributor.id))
        filepath = os.path.join(temp_dir, filename)

        if not os.path.exists(filepath):
            return HttpResponse("failure\nFile not found", content_type="text/plain")

        try:
            if "import" in filename:
                _process_cml_import(filepath, distributor)
            elif "offers" in filename:
                _process_cml_offers(filepath, distributor)

            return HttpResponse("success", content_type="text/plain")
        except Exception as e:
            return HttpResponse(f"failure\n{str(e)}", content_type="text/plain")

    return HttpResponse("failure\nUnknown mode", content_type="text/plain")


def _process_cml_import(filepath, distributor):
    """Parses import.xml (Catalog/Products)"""
    tree = ET.parse(filepath)
    root = tree.getroot()

    # Simple CML parsing (namespace agnostic for robustness)
    # Finding <Товар> elements
    for product_node in root.findall(".//{*}Товар"):
        ext_id = product_node.findtext("{*}Ид")
        name = product_node.findtext("{*}Наименование")
        sku = product_node.findtext("{*}Артикул")

        if not ext_id: continue

        # Split ID if it contains '#' (CML property separator)
        ext_id = ext_id.split("#")[0]

        Product.objects.update_or_create(
            distributor=distributor,
            external_id=ext_id,
            defaults={
                "name": name or "Без названия",
                "sku": sku or ext_id,
                "category": "1C Import",
                "is_active": True
            }
        )

def _process_cml_offers(filepath, distributor):
    """Parses offers.xml (Stock/Prices)"""
    tree = ET.parse(filepath)
    root = tree.getroot()

    for offer_node in root.findall(".//{*}Предложение"):
        ext_id = offer_node.findtext("{*}Ид")
        if not ext_id: continue
        ext_id = ext_id.split("#")[0]

        quantity = offer_node.findtext("{*}Количество")
        price_node = offer_node.find(".//{*}ЦенаЗаЕдиницу")

        update_fields = {}
        if quantity is not None:
            q = int(float(quantity))
            update_fields["quantity"] = q
            update_fields["status"] = "inStock" if q > 0 else "outOfStock"

        if price_node is not None:
            update_fields["price"] = Decimal(price_node.text.replace(",", "."))

        if update_fields:
            Product.objects.filter(distributor=distributor, external_id=ext_id).update(**update_fields)

@require_GET
def distributor_integration_token(request):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    if is_admin or distributor is None:
        return JsonResponse({"detail": "Используйте /api/admin/integration/tokens/ для администраторов"}, status=403)

    token = IntegrationToken.objects.filter(distributor=distributor, is_active=True).first()
    recent_logs = SyncLog.objects.filter(distributor=distributor).order_by("-created_at")[:20]
    return JsonResponse({
        "token": token.token if token else None,
        "createdAt": token.created_at.isoformat() if token else None,
        "logs": [
            {
                "id": log.id,
                "type": log.sync_type,
                "status": log.status,
                "details": log.details,
                "createdAt": log.created_at.isoformat(),
            }
            for log in recent_logs
        ],
    })


@csrf_exempt
@require_POST
def distributor_integration_generate(request):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    if is_admin or distributor is None:
        return JsonResponse({"detail": "Используйте /api/admin/integration/generate/ для администраторов"}, status=403)

    with transaction.atomic():
        distributor.integration_tokens.all().update(is_active=False)
        new_token = secrets.token_hex(32)
        IntegrationToken.objects.create(
            distributor=distributor,
            token=new_token,
            is_active=True,
        )

    return JsonResponse({"token": new_token})


@require_GET
def admin_integration_tokens(request):
    user, is_global, err = _require_manager_scope(request)
    if err:
        return err

    distributor_qs = Distributor.objects.all()
    distributor_qs = _filter_by_manager_scope(user, is_global, distributor_qs, region_path="managed_regions")

    # Efficiently fetch distributors with their active token
    from django.db.models import Prefetch
    active_tokens = IntegrationToken.objects.filter(is_active=True)
    distributors = distributor_qs.prefetch_related(
        Prefetch('integration_tokens', queryset=active_tokens, to_attr='active_tokens_list')
    ).distinct()

    def _format_token_row(d):
        token = d.active_tokens_list[0] if d.active_tokens_list else None
        return {
            "id": str(d.id),
            "name": d.name,
            "token": token.token if token else None,
            "createdAt": token.created_at.isoformat() if token else None,
        }

    return JsonResponse(paginated_response(request, distributors, _format_token_row))


@csrf_exempt
@require_POST
def admin_integration_generate(request, distributor_id):
    user, is_admin, err = _require_manager_scope(request)
    if err:
        return err
        
    distributor = Distributor.objects.filter(id=distributor_id).first()
    if not distributor:
        return JsonResponse({"detail": "Дистрибьютор не найден"}, status=404)
        
    try:
        with transaction.atomic():
            # Deactivate old tokens using all().update() for maximum compatibility
            distributor.integration_tokens.all().update(is_active=False)
            
            # Generate new 32-byte (64 hex chars) token
            new_token = secrets.token_hex(32)
            IntegrationToken.objects.create(
                distributor=distributor, 
                token=new_token,
                is_active=True
            )
            
            return JsonResponse({"token": new_token})
    except Exception as e:
        return JsonResponse({"detail": f"Ошибка генерации: {str(e)}"}, status=500)


@require_GET
def admin_integration_logs(request):
    user, is_global, err = _require_manager_scope(request)
    if err:
        return err
        
    qs = SyncLog.objects.select_related("distributor").order_by("-created_at")
    qs = _filter_by_manager_scope(user, is_global, qs, region_path="distributor__managed_regions")
    return JsonResponse(paginated_response(
        request,
        qs.distinct(),
        lambda log: {
            "id": str(log.id),
            "distributorName": log.distributor.name,
            "type": log.sync_type,
            "status": log.status,
            "details": log.details,
            "createdAt": log.created_at.isoformat(),
        },
    ))


@require_GET
def admin_analytics(request):
    user, is_global, err = _require_manager_scope(request)
    if err:
        return err
        
    from django.db.models import Sum
    from datetime import date
    
    today = date.today()
    start_of_month = today.replace(day=1)
    
    # Base Querysets
    client_qs = _filter_by_manager_scope(user, is_global, ClientProfile.objects.all()).distinct()
    purchase_qs = _filter_by_manager_scope(user, is_global, Purchase.objects.all(), "client__region").distinct()
    ticket_qs = _filter_by_manager_scope(user, is_global, ExpertTicket.objects.all(), "client__region").distinct()
    order_qs = _filter_by_manager_scope(user, is_global, Order.objects.all(), "client__region").distinct()
    sync_log_qs = _filter_by_manager_scope(user, is_global, SyncLog.objects.all(), "distributor__managed_regions").distinct()
    
    # System metrics (Global only)
    system_stats = {}
    if is_global:
        fulfilled_orders_qs = order_qs.filter(status="fulfilled")
        # Use aggregation for faster and memory-efficient summation
        order_total_agg = OrderItem.objects.filter(order__in=fulfilled_orders_qs).aggregate(total=Sum(F('price') * F('quantity')))
        total_fulfilled_value = order_total_agg['total'] or 0
        total_fulfilled_count = fulfilled_orders_qs.count()
        
        system_stats = {
            "totalManagers": Profile.objects.filter(role="manager").count(),
            "totalDistributors": Distributor.objects.count(),
            "totalRegions": Region.objects.count(),
            "avgOrderValue": float(total_fulfilled_value) / max(total_fulfilled_count, 1),
        }
        
    # Top 3 Distributors by turnover (Scoped for both Admin and Manager)
    top_distributors = []
    dist_turnover_agg = purchase_qs.filter(status="verified").values('distributor__name').annotate(turnover=Sum('total_amount')).order_by('-turnover')[:3]
    for item in dist_turnover_agg:
        top_distributors.append({
            "name": item['distributor__name'] or "N/A", 
            "value": float(item['turnover'] or 0)
        })
    system_stats["topDistributors"] = top_distributors

    # Apply filters from request
    region_id = request.GET.get("region")
    distributor_id = request.GET.get("distributor")
    
    if region_id:
        client_qs = client_qs.filter(region_id=region_id)
        purchase_qs = purchase_qs.filter(client__region_id=region_id)
        ticket_qs = ticket_qs.filter(client__region_id=region_id)
        order_qs = order_qs.filter(client__region_id=region_id)
        # For sync logs, we filter by distributor who has this region
        sync_log_qs = sync_log_qs.filter(distributor__managed_regions__id=region_id)

    if distributor_id:
        client_qs = client_qs.filter(distributor_id=distributor_id)
        purchase_qs = purchase_qs.filter(distributor_id=distributor_id)
        # ticket_qs = ticket_qs.filter(client__distributor_id=distributor_id) # ExpertTicket doesn't direct link to dist?
        order_qs = order_qs.filter(distributor_id=distributor_id)
        sync_log_qs = sync_log_qs.filter(distributor_id=distributor_id)

    total_clients = client_qs.count()
    total_purchases_month = purchase_qs.filter(
        status="verified", 
        date__gte=start_of_month
    ).aggregate(total=Sum('total_amount'))['total'] or 0
    
    active_tickets = ticket_qs.exclude(status="closed").count()
    new_orders_month = order_qs.filter(created_at__gte=start_of_month).count()
    
    total_syncs = sync_log_qs.count()
    successful_syncs = sync_log_qs.filter(status="success").count()
    sync_success_rate = (successful_syncs / total_syncs * 100) if total_syncs > 0 else 100.0

    # Recent actions
    recent_actions = []
    
    # 1. New clients
    for c in client_qs.order_by("-created_at")[:5]:
        recent_actions.append({
            "title": "Новый клиент",
            "subtitle": c.company_name,
            "time": c.created_at.strftime("%H:%M"),
            "timestamp": c.created_at
        })
        
    # 2. Tickets
    for t in ticket_qs.order_by("-created_at")[:5]:
        recent_actions.append({
            "title": "Тикет эксперту",
            "subtitle": f"#{t.id}: {t.category}",
            "time": t.created_at.strftime("%H:%M"),
            "timestamp": t.created_at
        })

    # 3. Syncs
    for s in sync_log_qs.select_related("distributor").order_by("-created_at")[:5]:
        recent_actions.append({
            "title": f"Синхронизация {s.distributor.name if s.distributor else 'ERP'}",
            "subtitle": f"Результат: {s.get_status_display()}",
            "time": s.created_at.strftime("%H:%M"),
            "timestamp": s.created_at
        })

    recent_actions.sort(key=lambda x: x["timestamp"], reverse=True)
    for a in recent_actions:
        del a["timestamp"] # Remove from response

    # Charts data
    turnover_chart = []
    for i in range(5, -1, -1):
        m = (today.month - i - 1) % 12 + 1
        y = today.year + (today.month - i - 1) // 12
        m_start = date(y, m, 1)
        if m == 12:
            m_end = date(y + 1, 1, 1)
        else:
            m_end = date(y, m + 1, 1)
            
        m_total = purchase_qs.filter(
            status="verified", 
            date__gte=m_start,
            date__lt=m_end
        ).aggregate(total=Sum('total_amount'))['total'] or 0
        
        turnover_chart.append({
            "label": m_start.strftime("%b"),
            "value": float(m_total)
        })

    categories_chart = []
    for cat_code, cat_label in [("a", "A"), ("b", "B"), ("c", "C")]:
        count = client_qs.filter(category=cat_code).count()
        if count > 0:
            categories_chart.append({"label": cat_label, "value": count})

    return JsonResponse({
        "totalClients": total_clients,
        "monthlyTurnover": float(total_purchases_month),
        "newOrders": new_orders_month,
        "openTickets": active_tickets,
        "syncSuccessRate": round(sync_success_rate, 1),
        "turnoverChart": turnover_chart,
        "categoriesChart": categories_chart,
        "recentActions": recent_actions[:10],
        "system": system_stats if is_global else None
    })


@require_GET
def distributor_couriers(request):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    # Get all users with courier role
    qs = User.objects.filter(profile__role="courier", is_active=True).order_by("username")

    def _format_courier(c):
        full_name = f"{c.first_name} {c.last_name}".strip()
        return {
            "id": str(c.id),
            "name": full_name if full_name else f"Курьер {c.username}",
            "phone": c.username,
        }

    return JsonResponse(paginated_response(request, qs, _format_courier))


@require_GET
def distributor_dashboard(request):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    clients = _scope_clients(distributor, is_admin)
    purchases = _scope_purchases(distributor, is_admin)
    orders_qs = _scope_orders(distributor, is_admin)
    delivery_qs = _scope_courier_tasks(distributor, is_admin)
    color_qs = ColorRequest.objects.filter(assigned_distributor=distributor) if not is_admin else ColorRequest.objects.all()
    
    # Standard statuses for verification
    to_verify = ["new", "pending", "pending_verification", "under_review", "duplicate_review"]
    
    return JsonResponse({
        "metrics": {
            "clients": clients.count(), 
            "purchasesToVerify": purchases.filter(status__in=to_verify).count(), 
            "ordersToProcess": orders_qs.filter(status="new").count(),
            "deliveriesToAssign": delivery_qs.filter(status="created").count(),
            "colorLabPending": color_qs.filter(status__in=["created", "pickedUp", "inProgress"]).count()
        }
    })


@require_GET
def distributor_color_requests(request):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    qs = ColorRequest.objects.all()
    if not is_admin:
        qs = qs.filter(assigned_distributor=distributor)
    
    status = request.GET.get("status")
    if status:
        qs = qs.filter(status=status)
    elif request.GET.get("active") == "true":
        # Только заявки в работе. Фильтруем на сервере: иначе при пагинации
        # страница могла бы целиком состоять из завершённых заявок.
        qs = qs.exclude(status__in=["delivered", "cancelled"])

    return JsonResponse(paginated_response(request, qs, _format_color_request))


@csrf_exempt
@require_POST
def distributor_update_color_request(request, request_id):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    
    item = ColorRequest.objects.filter(id=request_id).first()
    if not item:
        return JsonResponse({"detail": "Заявка не найдена"}, status=404)
        
    if not is_admin and item.assigned_distributor != distributor:
        return JsonResponse({"detail": "Access denied"}, status=403)
        
    payload = _json(request)
    new_status = payload.get("status")
    recipe = payload.get("recipe")
    
    if new_status:
        item.status = new_status
        _append_color_history(item, new_status, _current_user(request), payload.get("comment", "Статус обновлен дистрибьютором"))
        # Готовую краску и лючок маляр забирает сам: оттенок проверяют на месте,
        # и часть заявок сразу уходит в переделку с пояснениями колористу.
        # Поэтому обратной курьерской задачи (task_type="return") больше нет —
        # курьер участвует только в заборе лючка.

    if recipe is not None:
        item.recipe = recipe
        
    item.save()
    return JsonResponse({"request": _format_color_request(item)})


@require_GET
def distributor_clients(request):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    qs = _scope_clients(distributor, is_admin)
    search = (request.GET.get("search") or "").strip()
    if search:
        qs = qs.filter(Q(company_name__icontains=search) | Q(inn__icontains=search))
    return JsonResponse(paginated_response(request, qs, _format_client))


@require_GET
def distributor_purchases(request):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    
    # Список покупок для этого дистрибьютора
    qs = _scope_purchases(distributor, is_admin).order_by("-date")
    
    # Фильтрация по статусу
    status_filter = request.GET.get("status")
    to_verify_only = request.GET.get("to_verify") == "true"

    if status_filter:
        qs = qs.filter(status=status_filter)
    elif to_verify_only:
        # Show only what needs attention
        to_verify_list = ["new", "pending", "pending_verification", "under_review", "duplicate_review"]
        qs = qs.filter(status__in=to_verify_list)

    return JsonResponse(paginated_response(request, qs, _format_purchase))


import openpyxl
from openpyxl.styles import Font, Alignment
from django.http import HttpResponse

def _log_audit(request, action, obj, changes=None):
    AuditLog.objects.create(
        user=_current_user(request),
        action=action,
        model_name=obj.__class__.__name__,
        object_id=str(obj.id),
        changes=changes or {}
    )


@require_GET
def export_excel(request):
    user, is_global, err = _require_manager_scope(request)
    if err:
        return err
    
    region_id = request.GET.get("region")
    
    # Data
    qs = Purchase.objects.select_related("client", "client__region")
    qs = _filter_by_manager_scope(user, is_global, qs, region_path="client__region")
    
    if region_id:
        qs = qs.filter(client__region_id=region_id)
        
    # Create workbook
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "AutoTerra Report"
    
    # Headers
    headers = ["Регион", "Клиент", "ИНН", "Документ", "Дата", "Сумма", "Статус"]
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center")
    
    for row_idx, p in enumerate(qs.order_by("-date"), 2):
        ws.cell(row=row_idx, column=1, value=p.client.region.name if p.client.region else "-")
        ws.cell(row=row_idx, column=2, value=p.client.company_name)
        ws.cell(row=row_idx, column=3, value=p.client.inn)
        ws.cell(row=row_idx, column=4, value=p.document_number)
        ws.cell(row=row_idx, column=5, value=p.date.strftime("%d.%m.%Y") if p.date else "")
        ws.cell(row=row_idx, column=6, value=float(p.total_amount))
        ws.cell(row=row_idx, column=7, value=p.get_status_display())

    # Response
    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = 'attachment; filename="autoterra_report.xlsx"'
    wb.save(response)
    return response


@csrf_exempt
@require_http_methods(["PATCH", "POST"])
def distributor_verify_purchase(request, purchase_id):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    
    purchase = _scope_purchases(distributor, is_admin).filter(id=purchase_id).first()
    if not purchase:
        return JsonResponse({"detail": "Покупка не найдена"}, status=404)

    payload = _json(request)
    status = payload.get("status")
    reason = payload.get("rejection_reason")

    if status not in ["verified", "rejected"]:
        return JsonResponse({"detail": "Некорректный статус"}, status=400)

    old_status = purchase.status
    purchase.status = status
    if status == "rejected" and reason:
        purchase.rejection_reason = reason

    purchase.save()

    # Оборот вырос — возможно, вырос и ранг, а вместе с ним скидка клиента.
    sync_client_tier(purchase.client)

    _log_audit(request, f"Purchase status change: {old_status} -> {status}", purchase, {"reason": reason})

    return JsonResponse({"purchase": _format_purchase(purchase)})


@require_GET
def distributor_orders(request):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    
    qs = _scope_orders(distributor, is_admin).order_by("-created_at")

    status_filter = request.GET.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter)

    return JsonResponse(paginated_response(request, qs, _format_order))


@require_GET
def distributor_delivery_tasks(request):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    
    qs = _scope_courier_tasks(distributor, is_admin).order_by("-created_at")

    status_filter = request.GET.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter)
    elif request.GET.get("active") == "true":
        qs = qs.exclude(status__in=["delivered", "returned", "cancelled"])

    return JsonResponse(paginated_response(request, qs, _format_courier_task))


@csrf_exempt
@require_POST
def distributor_update_delivery_status(request, task_id):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    
    task = _scope_courier_tasks(distributor, is_admin).filter(id=task_id).first()
    if not task:
        return JsonResponse({"detail": "Заявка не найдена"}, status=404)
        
    payload = _json(request)
    status = payload.get("status")
    courier_id = payload.get("courierId")
    reason = payload.get("reason")
    
    old_status = task.status
    if status:
        task.status = status
    if courier_id:
        task.courier_id = courier_id
        if task.status == "created":
            task.status = "assigned"

    if status == "cancelled" and reason:
        # logic for reason if added to model, but CourierTask doesn't have it explicitly in models.py
        # we can log it in history
        pass

    _append_task_history(task, task.status, _current_user(request), f"Обновлено дистрибьютором. Статус: {task.status}, Курьер: {courier_id}")
    task.save()

    # Раньше назначение курьера на задачу возврата закрывало заявку Color Lab
    # («Выдана»). Возврат курьером отменён — готовое маляр забирает сам, и
    # закрыть заявку может только выдача на месте. Синхронизация снята, чтобы
    # назначенный на ЗАБОР лючка курьер не помечал заявку выданной.

    return JsonResponse({"task": _format_courier_task(task)})


@csrf_exempt
@require_http_methods(["PATCH", "POST"])
def distributor_update_order_status(request, order_id):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    
    order = _scope_orders(distributor, is_admin).filter(id=order_id).first()
    if not order:
        return JsonResponse({"detail": "Заказ не найден"}, status=404)

    payload = _json(request)
    status = payload.get("status")
    reason = payload.get("rejection_reason")
    courier_id = payload.get("courier_id")
    estimated_delivery_date = payload.get("estimated_delivery_date")

    if status not in ["new", "accepted", "rejected", "fulfilled"]:
        return JsonResponse({"detail": "Некорректный статус"}, status=400)

    old_status = order.status
    order.status = status
    if status == "rejected" and reason:
        order.rejection_reason = reason
        
    # Restore stock if order was NOT rejected before but IS rejected now
    if status == "rejected" and old_status != "rejected":
        with transaction.atomic():
            for item in order.items.all():
                product = item.product
                product.quantity += item.quantity
                if product.quantity > 5:
                    product.status = "inStock"
                elif product.quantity > 0:
                    product.status = "low"
                # Else stays onOrder or whatever it was
                product.save(update_fields=["quantity", "status"])
        
    if courier_id:
        try:
            courier = User.objects.get(id=courier_id)
            order.courier = courier
        except User.DoesNotExist:
            pass
    elif "courier_id" in payload and courier_id is None:
        order.courier = None

    if estimated_delivery_date:
        from datetime import datetime
        try:
            order.estimated_delivery_date = datetime.strptime(estimated_delivery_date, "%Y-%m-%d").date()
        except ValueError:
            pass
    elif "estimated_delivery_date" in payload and estimated_delivery_date is None:
        order.estimated_delivery_date = None
    
    order.save()

    _log_audit(request, f"Order status change: {old_status} -> {status}", order, {"reason": reason})
    _send_order_status_email(order, old_status, status)

    return JsonResponse({"order": _format_order(order)})


@require_GET
def distributor_stock(request):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    qs = _scope_products(distributor, is_admin)
    search = (request.GET.get("search") or "").strip()
    if search:
        qs = _search_products(qs, search)
    category = (request.GET.get("category") or "").strip()
    if category:
        qs = qs.filter(category=category)
    return JsonResponse(paginated_response(request, qs, _format_product))


@csrf_exempt
@require_POST
def distributor_add_product(request):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    if is_admin:
        return JsonResponse({"detail": "Admin must specify distributorId"}, status=400)
        
    payload = _json(request)
    sku = payload.get("sku")
    if not sku:
        return JsonResponse({"detail": "SKU обязателен"}, status=400)
        
    if Product.objects.filter(distributor=distributor, sku=sku).exists():
        return JsonResponse({"detail": f"Товар с артикулом {sku} уже существует"}, status=400)
        
    product = Product.objects.create(
        distributor=distributor,
        sku=sku,
        name=payload.get("name", "Новый товар"),
        category=payload.get("category", "Общее"),
        brand=payload.get("brand", "AutoTerra"),
        description=payload.get("description", ""),
        # Фото — только ссылки, максимум Product.MAX_IMAGES (лишние отсекаются).
        images=normalize_product_images(payload.get("images")),
        video_url=payload.get("videoUrl") or "",
        price=_money_value(payload.get("price")),
        quantity=int(payload.get("quantity", 0)),
        status=payload.get("status", "inStock")
    )
    
    return JsonResponse({"status": "ok", "product": _format_product(product)})


@csrf_exempt
@require_POST
def distributor_stock_upload(request):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    if is_admin:
        return JsonResponse({"detail": "Admin must specify distributorId"}, status=400)
    items = _json(request).get("items", [])
    with transaction.atomic():
        for raw in items:
            defaults = {
                "name": raw.get("name"),
                "category": raw.get("category"),
                "brand": raw.get("brand", "AutoTerra"),
                "price": _money_value(raw.get("price")),
                "quantity": int(raw.get("quantity", 0)),
                "status": raw.get("status", "inStock"),
            }
            # Фото передаём только если ключ есть — иначе не затираем уже
            # загруженные из Excel ссылки.
            if "images" in raw:
                defaults["images"] = normalize_product_images(raw.get("images"))
            Product.objects.update_or_create(
                distributor=distributor,
                sku=raw.get("sku"),
                defaults=defaults,
            )
    return JsonResponse({"status": "ok", "processed": len(items)})


@csrf_exempt
@require_POST
def distributor_stock_upload_file(request):
    """Загрузка ассортимента из Excel-файла (шаблон WB «Общие характеристики»).

    Принимает multipart-файл в поле ``file``. Парсинг и апсерт выполняются на
    сервере — единый разбор для мобильного приложения и админки.
    Характеристики обновляются всегда; цена/остаток — только если такие колонки
    есть в файле (иначе существующие значения сохраняются).
    """
    from api.services.product_import import parse_products_workbook, upsert_products

    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    if is_admin:
        return JsonResponse({"detail": "Admin must specify distributorId"}, status=400)

    file_obj = request.FILES.get("file")
    if not file_obj:
        return JsonResponse({"detail": "Файл не передан (поле 'file')"}, status=400)

    products, errors = parse_products_workbook(file_obj)
    if not products:
        return JsonResponse(
            {"detail": errors[0] if errors else "В файле не найдено товаров.", "errors": errors},
            status=400,
        )

    with transaction.atomic():
        created, updated = upsert_products(distributor, products)

    return JsonResponse({
        "status": "ok",
        "created": created,
        "updated": updated,
        "processed": created + updated,
        "errors": errors,
    })


@require_GET
def distributor_reports(request):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    return JsonResponse({"summary": "Stock and order reports will be here"})


# Referral Views

@require_GET
def referrals(request):
    client, err = _require_client(request)
    if err:
        return err
        
    # Данные приглашённых подтягиваем из их профилей: покупка могла случиться
    # уже после создания записи.
    for referral in client.referrals.all():
        referral.sync_from_invitee()

    qs = client.referrals.all()
    stats = {
        "invitedCount": qs.count(),
        "registeredCount": qs.filter(is_registered=True).count(),
        "buyersCount": qs.filter(has_purchase=True).count(),
        # Выданным считается только согласованный подарок: до решения
        # дистрибьютора обещать клиенту нечего (п. 7 ТЗ).
        "giftCount": qs.filter(condition_met=True, gift_status="approved").count(),
        "pendingGiftCount": qs.filter(gift_status="pending").count(),
        "purchaseAmount": float(sum(r.purchase_amount for r in qs)),
    }
    base_url = getattr(settings, "REFERRAL_INVITE_BASE_URL", "https://autoterra.shop/register")
    return JsonResponse(paginated_response(
        request,
        qs.order_by("-created_at"),
        _format_referral,
        extra={
            "stats": stats,
            # Личный код и готовая ссылка — их клиент и отправляет коллегам.
            "referralCode": client.referral_code,
            "inviteLink": f"{base_url}?ref={client.referral_code}",
            "bonusThreshold": float(getattr(settings, "REFERRAL_BONUS_THRESHOLD", 30000)),
            "bonusGift": getattr(settings, "REFERRAL_BONUS_GIFT", ""),
            # Правило проверки ИНН держим на сервере и отдаём приложению:
            # две независимые реализации разъезжаются, и форма начинает
            # отклонять то, что сервер принял бы.
            "strictInn": _strict_inn_enabled(),
        },
    ))

@csrf_exempt
@require_POST
def create_referral(request):
    """Ручная заявка на приглашённое СТО.

    Заявка закрепляет за клиентом ещё не зарегистрированное СТО по ИНН —
    бонус зачтётся, даже если оно придёт без реферального кода. Раз запись
    сама по себе даёт право на подарок, тут нужны все проверки: раньше их не
    было вообще, и по одному ИНН можно было заявиться самому себе, дважды и
    поверх чужой заявки.
    """
    client, err = _require_client(request)
    if err:
        return err
    payload = _json(request)

    inn = str(payload.get("inviteeInn") or "").strip()
    name = str(payload.get("inviteeName") or "").strip()

    if not name:
        return JsonResponse({"detail": "Укажите название СТО"}, status=400)
    if not inn.isdigit() or len(inn) not in (10, 12):
        return JsonResponse(
            {"detail": "ИНН — 10 цифр для организации, 12 для ИП"}, status=400
        )
    # Контрольная сумма, а не только длина: по выдуманному ИНН запись никогда
    # ни с кем не совпадёт и клиент будет впустую ждать бонус.
    #
    # Отключается настройкой: в базе с демо-данными (ООО Ромашка и подобные)
    # реальных ИНН нет, и строгая проверка не даёт ничего протестировать.
    if _strict_inn_enabled() and not is_valid_inn(inn):
        return JsonResponse(
            {"detail": "ИНН указан неверно — проверьте цифры"}, status=400
        )
    if inn == (client.inn or "").strip():
        return JsonResponse(
            {"detail": "Нельзя пригласить самого себя"}, status=400
        )

    if Referral.objects.filter(inviter=client, invitee_inn=inn).exists():
        return JsonResponse(
            {"detail": "Это СТО уже есть в вашем списке приглашений"}, status=409
        )
    # Одно СТО — один пригласивший, иначе подарок уйдёт дважды за одного клиента.
    if Referral.objects.filter(invitee_inn=inn).exists():
        return JsonResponse(
            {"detail": "Это СТО уже заявлено другим участником программы"}, status=409
        )
    # Заявиться можно только на того, кто ещё не пришёл. Иначе достаточно было
    # бы вписать ИНН любого действующего клиента и забрать бонус за чужого.
    if ClientProfile.objects.filter(inn=inn).exists():
        return JsonResponse(
            {
                "detail": "СТО с таким ИНН уже зарегистрировано в AutoTerra — "
                "заявку можно подать только на нового участника"
            },
            status=409,
        )

    # region у Referral — строка, а у клиента это ссылка на справочник.
    # Раньше сюда клали сам объект Region и полагались на его __str__.
    region = payload.get("region") or (client.region.name if client.region_id else "")

    item = Referral.objects.create(
        inviter=client,
        invitee_inn=inn,
        invitee_name=name,
        region=region,
        # Заявка сама себя не подтверждает: право на подарок появится, только
        # когда приглашённое СТО подтвердит это при регистрации.
        confirmation="pending",
    )
    return JsonResponse({"referral": _format_referral(item)}, status=201)


@csrf_exempt
@require_POST
def confirm_referral(request, referral_id):
    """Приглашённый подтверждает или отклоняет заявку на себя.

    Последнее звено защиты: ручную заявку по чужому ИНН может подать кто
    угодно, и до этого решения она не даёт права на подарок.
    """
    client, err = _require_client(request)
    if err:
        return err

    referral = Referral.objects.filter(id=referral_id).select_related("inviter").first()
    if referral is None:
        return JsonResponse({"detail": "Приглашение не найдено"}, status=404)
    # Решать может только тот, кого заявили. Сравниваем по ИНН — именно по нему
    # заявку и подавали.
    if referral.invitee_inn != client.inn:
        return JsonResponse({"detail": "Это приглашение адресовано не вам"}, status=403)
    if referral.confirmation != "pending":
        return JsonResponse(
            {"detail": "Решение уже принято", "confirmation": referral.confirmation},
            status=409,
        )

    confirmed = bool(_json(request).get("confirmed"))
    referral.confirmation = "confirmed" if confirmed else "declined"
    referral.confirmed_at = timezone.now()
    referral.save(update_fields=["confirmation", "confirmed_at"])
    if confirmed:
        # Покупки могли быть и до подтверждения — пересчитываем сразу.
        referral.sync_from_invitee()
    return JsonResponse({"referral": _format_referral(referral)})


@require_GET
def distributor_referral_gifts(request):
    """Подарки, ждущие согласования, — по клиентам своего региона (п. 7 ТЗ)."""
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err

    qs = Referral.objects.filter(gift_status="pending").select_related("inviter")
    if not is_admin:
        qs = qs.filter(inviter__distributor=distributor)

    def _format(item):
        data = _format_referral(item)
        data["inviterName"] = item.inviter.company_name
        data["inviterInn"] = item.inviter.inn
        # Согласующему нужен сам подарок, даже пока он не выдан.
        data["proposedGift"] = item.gift or None
        return data

    return JsonResponse(paginated_response(request, qs.order_by("-created_at"), _format))


@csrf_exempt
@require_POST
def decide_referral_gift(request, referral_id):
    """Дистрибьютор согласовывает или отклоняет подарок пригласившему."""
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err

    referral = Referral.objects.filter(id=referral_id).select_related("inviter").first()
    if referral is None:
        return JsonResponse({"detail": "Реферал не найден"}, status=404)
    if not is_admin and referral.inviter.distributor_id != getattr(distributor, "id", None):
        return JsonResponse({"detail": "Клиент не из вашего региона"}, status=403)
    if referral.gift_status != "pending":
        return JsonResponse(
            {"detail": "Решение уже принято", "giftStatus": referral.gift_status}, status=409
        )

    payload = _json(request)
    approved = bool(payload.get("approved"))
    referral.gift_status = "approved" if approved else "declined"
    referral.gift_comment = (payload.get("comment") or "").strip()[:255]
    referral.gift_decided_by = _current_user(request)
    referral.gift_decided_at = timezone.now()
    # Уведомление отправит сигнал _referral_post_save — так оно уходит и при
    # согласовании из админки, а не только отсюда.
    referral.save(update_fields=["gift_status", "gift_comment", "gift_decided_by", "gift_decided_at"])
    return JsonResponse({"referral": _format_referral(referral)})


@require_GET
def purchase_analytics(request):
    """Аналитика закупок для менеджера: срезы или выгрузка файлом.

    Считает тот же сервис, что и страница в админке. Менеджер видит только
    свои регионы — глобальный видит всю Россию.

    Фильтры: date_from, date_to, distributor, region, sku, status.
    Выгрузка: ?export=xlsx | csv
    """
    from api.services import purchase_analytics as analytics
    from api.services.exports import ExportUnavailable

    user, is_global, err = _require_manager_scope(request)
    if err:
        return err

    regions = _get_manager_regions(user, is_global)
    allowed = None if regions is None else list(regions.values_list("id", flat=True))
    filters = analytics.Filters.from_request(request, allowed_region_ids=allowed)

    export_format = request.GET.get("export")
    if export_format:
        try:
            return analytics.export(filters, export_format)
        except ExportUnavailable as exc:
            return JsonResponse({"detail": str(exc)}, status=503)

    data = analytics.report(filters)
    totals = data["totals"]
    return JsonResponse({
        "totals": {
            "count": totals["count"],
            "amount": float(totals["amount"]),
            "average": float(totals["average"]),
            "clients": totals["clients"],
            "quantity": totals["quantity"],
        },
        "bySku": [
            {
                "sku": row["sku"],
                "name": row["name"],
                "quantity": row["quantity"],
                "amount": float(row["amount"]),
                "documents": row["documents"],
            }
            for row in data["bySku"]
        ],
        "byClient": [
            {
                "name": row["client__company_name"],
                "inn": row["client__inn"],
                "region": row["client__region__name"] or "",
                "documents": row["documents"],
                "amount": float(row["amount"]),
            }
            for row in data["byClient"]
        ],
        "byRegion": [
            {
                "region": row["client__region__name"] or "",
                "clients": row["clients"],
                "documents": row["documents"],
                "amount": float(row["amount"]),
            }
            for row in data["byRegion"]
        ],
        "byDistributor": [
            {
                "distributor": row["distributor__name"] or "",
                "documents": row["documents"],
                "amount": float(row["amount"]),
            }
            for row in data["byDistributor"]
        ],
        "byMonth": [
            {
                "month": row["month"].strftime("%Y-%m") if row["month"] else "",
                "documents": row["documents"],
                "amount": float(row["amount"]),
            }
            for row in data["byMonth"]
        ],
        "filters": filters.as_dict(),
    })


@require_GET
def bonus_account(request):
    """Баланс бонусов и история операций по нему."""
    client, err = _require_client(request)
    if err:
        return err

    def _format(item):
        return {
            "id": str(item.id),
            "amount": float(item.amount),
            "kind": item.kind,
            "kindLabel": item.get_kind_display(),
            "comment": item.comment,
            "orderId": str(item.order_id) if item.order_id else None,
            "createdAt": item.created_at.isoformat(),
        }

    qs = BonusTransaction.objects.filter(client=client).order_by("-created_at")
    return JsonResponse(paginated_response(
        request, qs, _format, extra={"balance": float(bonus_balance(client))}
    ))


# Learning Materials (п. 10 ТЗ)

def _format_learning_material(item):
    return {
        "id": str(item.id),
        "title": item.title,
        "kind": item.kind,
        "kindLabel": item.get_kind_display(),
        "category": item.category,
        "summary": item.summary,
        "body": item.body,
        "videoUrl": item.video_url or None,
        "fileUrl": item.file_url or None,
        "durationMinutes": item.duration_minutes,
        "status": item.status,
        "createdAt": item.created_at.isoformat(),
    }


@require_GET
def learning_materials(request):
    """Материалы для клиента. Черновики и архив видят только эксперты."""
    user = _current_user(request)
    if user is None:
        return JsonResponse({"detail": "Unauthorized"}, status=401)

    qs = LearningMaterial.objects.all()
    if not _is_expert_user(user):
        qs = qs.filter(status="published")

    kind = (request.GET.get("kind") or "").strip()
    if kind:
        qs = qs.filter(kind=kind)
    category = (request.GET.get("category") or "").strip()
    if category:
        qs = qs.filter(category__iexact=category)

    return JsonResponse(paginated_response(
        request, qs.order_by("-created_at"), _format_learning_material
    ))


# Support & Expert Views

@require_GET
def tickets(request):
    client, err = _require_client(request)
    if err:
        # Check if it's an expert
        user, is_admin, expert_err = _require_expert_scope(request)
        if expert_err:
            return err # Original unauthorized error
        # It's an expert, return all tickets or filtered
        qs = ExpertTicket.objects.select_related("client").order_by("-created_at")
        return JsonResponse(paginated_response(request, qs, _format_ticket))

    # It's a client, return only their tickets
    qs = client.expert_tickets.order_by("-created_at")
    return JsonResponse(paginated_response(request, qs, _format_ticket))


@csrf_exempt
@require_POST
def create_ticket(request):
    client, err = _require_client(request)
    if err:
        return err
    payload = request.POST if (request.content_type or "").startswith("multipart/form-data") else _json(request)
    files = _attachment_files(request)
    with transaction.atomic():
        item = ExpertTicket.objects.create(
            client=client,
            question=(payload.get("question") or "").strip(),
            category=(payload.get("category") or "General").strip(),
            risk=payload.get("risk", "low"),
            status="open",
        )
        _create_attachments(request, item, files, description="Вложения к тикету")
        
        # Simple AI draft generation logic
        cards = KnowledgeCard.objects.filter(status="approved").filter(
            Q(problem__icontains=item.question) | Q(title__icontains=item.question)
        )
        if cards.exists():
            card = cards.first()
            item.ai_draft_answer = f"Предположительный ответ на основе базы знаний:\n{card.solution}"
            item.similar_cases = [str(card.id)]
            item.save(update_fields=["ai_draft_answer", "similar_cases"])

    return JsonResponse({"ticket": _format_ticket(item)}, status=201)


@csrf_exempt
@require_POST
def expert_answer_ticket(request, ticket_id):
    user, is_admin, err = _require_expert_scope(request)
    if err:
        return err
    payload = _json(request)
    answer = (payload.get("answer") or "").strip()
    causes = (payload.get("causes") or "").strip()
    status = payload.get("status", "expertAnswered")
    
    ticket = ExpertTicket.objects.filter(id=ticket_id).first()
    if not ticket:
        return JsonResponse({"detail": "Тикет не найден"}, status=404)
        
    with transaction.atomic():
        ticket.expert_answer = answer
        ticket.expert_author = user
        ticket.status = status
        ticket.save(update_fields=["expert_answer", "expert_author", "status"])
        
        # По п. 9 ТЗ черновик карточки формируется сам, а эксперт его потом
        # утверждает, редактирует или отклоняет. Публикации без утверждения не
        # происходит: карточка создаётся в статусе draft, а AI отвечает только
        # по approved. Явный createKnowledgeCard=false позволяет отказаться —
        # например, когда вопрос разовый и знанием не станет.
        wants_card = payload.get("createKnowledgeCard")
        auto_draft = (
            answer
            and ticket.category
            and ticket.linked_knowledge_card_id is None
        )
        if wants_card is None:
            wants_card = bool(auto_draft)

        if wants_card:
            card = KnowledgeCard.objects.create(
                title=f"Кейс: {ticket.category}",
                category=ticket.category,
                problem=ticket.question,
                causes=causes,
                solution=answer,
                status="draft",
                expert_author=user,
                source_ticket=ticket,
            )
            ticket.linked_knowledge_card = card
            ticket.save(update_fields=["linked_knowledge_card"])
            
    return JsonResponse({"ticket": _format_ticket(ticket)})


@require_GET
def knowledge_cards(request):
    user = _current_user(request)
    # Check if user is expert to see drafts
    if _is_expert_user(user):
        # Эксперту неодобренные карточки показываем первыми. Сортировка должна
        # быть на сервере: отсортировать одну страницу на клиенте недостаточно.
        qs = KnowledgeCard.objects.all().order_by(
            models.Case(
                models.When(status="approved", then=models.Value(1)),
                default=models.Value(0),
                output_field=models.IntegerField(),
            ),
            "-created_at",
        )
    else:
        qs = KnowledgeCard.objects.filter(status="approved")
    search = (request.GET.get("search") or "").strip()
    if search:
        qs = qs.filter(Q(problem__icontains=search) | Q(solution__icontains=search))
    return JsonResponse(paginated_response(request, qs, _format_knowledge_card))


@csrf_exempt
@require_POST
def create_knowledge_card(request):
    user, is_admin, err = _require_expert_scope(request)
    if err:
        return err
    payload = _json(request)
    
    card = KnowledgeCard.objects.create(
        title=payload.get("title", ""),
        category=payload.get("category", ""),
        problem=payload.get("problem", ""),
        causes=payload.get("causes", ""),
        solution=payload.get("solution", ""),
        status=payload.get("status", "draft"),
        skus=payload.get("skus", []),
        restrictions=payload.get("restrictions", ""),
        expert_author=user
    )
    return JsonResponse({"card": _format_knowledge_card(card)})


@csrf_exempt
@require_POST
def update_knowledge_card(request, card_id):
    user, is_admin, err = _require_expert_scope(request)
    if err:
        return err
    payload = _json(request)
    card = KnowledgeCard.objects.filter(id=card_id).first()
    if not card:
        return JsonResponse({"detail": "Карточка не найдена"}, status=404)
        
    fields = ["title", "category", "problem", "causes", "solution", "status", "skus", "restrictions"]
    updated_fields = []
    for f in fields:
        if f in payload:
            setattr(card, f, payload[f])
            updated_fields.append(f)
            
    if payload.get("status") == "approved":
        card.status = "approved"
        if "status" not in updated_fields:
            updated_fields.append("status")
        
    if updated_fields:
        card.save(update_fields=updated_fields)
    else:
        card.save()
    return JsonResponse({"card": _format_knowledge_card(card)})


@csrf_exempt
@require_POST
def register_device_token(request):
    """Save or refresh the FCM token for the authenticated user's device."""
    user = _current_user(request)
    if user is None:
        return JsonResponse({"detail": "Unauthorized"}, status=401)

    payload = _json(request)
    token = (payload.get("token") or "").strip()
    platform = payload.get("platform", "android")

    if not token:
        return JsonResponse({"detail": "token is required"}, status=400)
    if platform not in ("android", "ios"):
        return JsonResponse({"detail": "platform must be 'android' or 'ios'"}, status=400)

    from api.models import UserDeviceToken

    obj, created = UserDeviceToken.objects.update_or_create(
        token=token,
        defaults={"user": user, "platform": platform},
    )
    return JsonResponse({"status": "created" if created else "updated"})


@csrf_exempt
@require_POST
def send_notification(request):
    user = _current_user(request)
    profile = getattr(user, "profile", None)
    if not profile or profile.role not in ["admin", "manager", "ai_expert"]:
        return JsonResponse({"detail": "Forbidden"}, status=403)

    payload = _json(request)
    target_user_id = payload.get("userId")
    title = payload.get("title")
    body = payload.get("body")
    n_type = payload.get("type", "info")
    link = payload.get("relatedLink", "")

    if not all([target_user_id, title, body]):
        return JsonResponse({"detail": "Missing fields"}, status=400)

    target_user = User.objects.filter(id=target_user_id).first()
    if not target_user:
        return JsonResponse({"detail": "User not found"}, status=404)

    notification = Notification.objects.create(
        user=target_user,
        title=title,
        body=body,
        type=n_type,
        related_link=link,
    )

    from api.services.push_notifications import PushNotificationService
    push_result = PushNotificationService().send(notification)

    return JsonResponse({"status": "ok", "push": push_result})


@require_GET
def push_diagnostics(request):
    """
    Admin-only: diagnose the full FCM push pipeline.

    Returns:
      - firebase_ok: whether Firebase Admin SDK initialises without error
      - device_tokens: tokens registered for the calling user
      - send_result: result of a live test push (if ?send=1 is passed)

    Usage from PythonAnywhere bash:
      curl -H "Authorization: Bearer <token>" \
           "https://sigmaadil.pythonanywhere.com/api/debug/push/?send=1"
    """
    user = _current_user(request)
    if user is None:
        return JsonResponse({"detail": "Unauthorized"}, status=401)
    profile = getattr(user, "profile", None)
    if not (user.is_staff or user.is_superuser or (profile and profile.role in ("admin", "manager"))):
        return JsonResponse({"detail": "Forbidden"}, status=403)

    from api.models import UserDeviceToken, Notification
    from api.services.push_notifications import PushNotificationService, _get_firebase_app

    report: dict = {}

    # 1. Firebase SDK init
    try:
        app = _get_firebase_app()
        report["firebase_ok"] = True
        report["firebase_app_name"] = app.name
    except Exception as exc:
        report["firebase_ok"] = False
        report["firebase_error"] = str(exc)

    # 2. Device tokens for this user
    tokens = list(UserDeviceToken.objects.filter(user=user).values("token", "platform", "created_at"))
    report["device_token_count"] = len(tokens)
    report["device_tokens"] = [{"platform": t["platform"], "suffix": t["token"][-8:]} for t in tokens]

    # 3. Optional live test push
    if request.GET.get("send") == "1":
        if not tokens:
            report["send_result"] = "skipped — no device tokens registered for this user"
        else:
            try:
                notification = Notification.objects.create(
                    user=user,
                    title="FCM диагностика",
                    body="Если вы видите это — push работает корректно ✓",
                    type="system",
                )
                result = PushNotificationService().send(notification)
                report["send_result"] = result
            except Exception as exc:
                report["send_result"] = {"error": str(exc)}

    return JsonResponse(report)


@require_GET
def regions(request):
    user = _current_user(request)
    qs = Region.objects.filter(is_active=True).order_by("name")

    if user:
        profile = getattr(user, "profile", None)
        is_global = _is_user_global(user, profile)
        if not is_global and profile and profile.role == "manager":
            qs = qs.filter(manager=user)
        
    return JsonResponse(paginated_response(
        request,
        qs.distinct(),
        lambda item: {
            "id": str(item.id),
            "code": item.code,
            "name": item.name,
            "active": item.is_active,
        },
    ))


@require_GET
def notifications(request):
    user = _current_user(request)
    if user is None:
        return JsonResponse({"detail": "Unauthorized"}, status=401)
        
    qs = user.notifications.order_by("-created_at")
    return JsonResponse(paginated_response(
        request,
        qs,
        _format_notification,
        extra={"unreadCount": user.notifications.filter(is_read=False).count()},
    ))


@csrf_exempt
@require_POST
def mark_notifications_read(request):
    user = _current_user(request)
    if user is None:
        return JsonResponse({"detail": "Unauthorized"}, status=401)
    user.notifications.filter(is_read=False).update(is_read=True)
    return JsonResponse({"ok": True})


@csrf_exempt
def manager_clients(request):
    if request.method == 'GET':
        user, is_global, err = _require_manager_scope(request)
        if err:
            return err

        qs = ClientProfile.objects.all().select_related('region', 'distributor')
        qs = _filter_by_manager_scope(user, is_global, qs)

        status_filter = request.GET.get('status')
        category_filter = request.GET.get('category')
        region_filter = request.GET.get('region')
        if status_filter:
            qs = qs.filter(status=status_filter)
        if category_filter:
            qs = qs.filter(category=category_filter.lower())
        if region_filter:
            qs = qs.filter(region_id=region_filter)

        search = (request.GET.get('search') or '').strip()
        if search:
            qs = qs.filter(Q(company_name__icontains=search) | Q(inn__icontains=search))

        return JsonResponse(paginated_response(request, qs, _format_manager_client))

    if request.method == 'POST':
        user, is_global, err = _require_manager_scope(request)
        if err:
            return err

        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'detail': 'Invalid JSON'}, status=400)

        inn = data.get('inn', '').strip()
        if not inn or not inn.isdigit() or len(inn) not in (10, 12):
            return JsonResponse({'detail': 'ИНН должен содержать 10 или 12 цифр'}, status=400)

        company_name = data.get('name', '').strip()
        if not company_name:
            return JsonResponse({'detail': 'Название обязательно'}, status=400)

        region_id = data.get('regionId')
        if not region_id:
            return JsonResponse({'detail': 'Регион обязателен'}, status=400)

        try:
            region = Region.objects.get(id=region_id)
        except Region.DoesNotExist:
            return JsonResponse({'detail': 'Регион не найден'}, status=404)

        if ClientProfile.objects.filter(inn=inn, region=region).exists():
            return JsonResponse(
                {'detail': f'Клиент с ИНН {inn} уже зарегистрирован в регионе {region.name}'},
                status=409,
            )

        # Optional: manager may specify a distributor; otherwise auto-assigned from region
        explicit_distributor = None
        distributor_id = data.get('distributorId')
        if distributor_id:
            try:
                explicit_distributor = Distributor.objects.get(id=distributor_id)
            except Distributor.DoesNotExist:
                return JsonResponse({'detail': 'Дистрибьютор не найден'}, status=404)

        phone = data.get('phone', '').strip()
        email = data.get('email', '').strip()
        category = data.get('category', 'b').lower()
        city = data.get('city', '').strip()
        contact_name = data.get('contact', '').strip() or company_name

        # Build a unique username from INN + region code
        base_username = f"client_{inn}_{region.code or region_id}"
        username = base_username
        suffix = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}_{suffix}"
            suffix += 1

        try:
            with transaction.atomic():
                user_account = User.objects.create_user(
                    username=username,
                    password=get_random_string(16),
                    first_name=company_name[:30],
                    email=email,
                )
                Profile.objects.filter(user=user_account).update(role='client')

                client = ClientProfile(
                    user=user_account,
                    inn=inn,
                    company_name=company_name,
                    category=category,
                    region=region,
                    city=city,
                    contact_name=contact_name,
                    phone=phone,
                    manager=user,
                    registration_source='manager',
                    status='new',
                )
                # Explicit distributor overrides auto-assign from region
                if explicit_distributor:
                    client.distributor = explicit_distributor
                # save() runs auto-assign logic only when distributor is still None
                client.save()
        except IntegrityError as exc:
            return JsonResponse({'detail': f'Ошибка: {exc}'}, status=409)
        except Exception as exc:
            return JsonResponse({'detail': str(exc)}, status=400)

        return JsonResponse({'client': _format_manager_client(client)}, status=201)

    return JsonResponse({'detail': 'Method not allowed'}, status=405)


@require_GET
def manager_client_unified(request, client_id):
    user, is_global, err = _require_manager_scope(request)
    if err:
        return err
    
    client = get_object_or_404(ClientProfile, id=client_id)
    
    # Check scope
    managed_regions = _get_manager_regions(user, is_global)
    if managed_regions is not None and client.region not in managed_regions:
        return JsonResponse({"detail": "Доступ запрещен (вне вашей зоны ответственности)"}, status=403)
    
    # 1. Profile
    profile_data = _format_client(client)
    
    # 2. Purchases
    purchases = [_format_purchase(p) for p in _limit(request, client.purchases.all().order_by("-date"))]
    
    # 3. Orders
    orders = [_format_order(o) for o in _limit(request, client.orders.all().order_by("-created_at"))]
    
    # 4. Color Requests
    color_requests = [_format_color_request(c) for c in _limit(request, client.color_requests.all().order_by("-created_at"))]
    
    # 5. Expert Tickets
    tickets = [_format_ticket(t) for t in _limit(request, client.expert_tickets.all().order_by("-created_at"))]
    
    # 6. Referrals
    referrals = [_format_referral(r) for r in _limit(request, client.referrals.all().order_by("-created_at"))]
    
    return JsonResponse({
        "client": profile_data,
        "purchases": purchases,
        "orders": orders,
        "colorRequests": color_requests,
        "tickets": tickets,
        "referrals": referrals,
    })


@csrf_exempt
@require_http_methods(['POST', 'PUT', 'PATCH'])
def manager_client_status(request, client_id):
    user, is_global, err = _require_manager_scope(request)
    if err:
        return err

    client = get_object_or_404(ClientProfile.objects.select_related('region', 'distributor'), id=client_id)

    managed_regions = _get_manager_regions(user, is_global)
    if managed_regions is not None and client.region not in managed_regions:
        return JsonResponse({'detail': 'Доступ запрещён'}, status=403)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'detail': 'Invalid JSON'}, status=400)

    new_status = data.get('status')
    valid_statuses = [s[0] for s in ClientProfile.STATUS_CHOICES]
    if new_status not in valid_statuses:
        return JsonResponse({'detail': f'Недопустимый статус. Допустимые: {valid_statuses}'}, status=400)

    client.status = new_status
    if new_status == 'active' and not client.distributor and client.region and client.region.distributor:
        client.distributor = client.region.distributor

    client.save(update_fields=['status', 'distributor'])
    return JsonResponse({'client': _format_manager_client(client)})


@csrf_exempt
def manager_client_history(request, client_id):
    user, is_global, err = _require_manager_scope(request)
    if err:
        return err

    client = get_object_or_404(ClientProfile.objects.select_related('region'), id=client_id)

    managed_regions = _get_manager_regions(user, is_global)
    if managed_regions is not None and client.region not in managed_regions:
        return JsonResponse({'detail': 'Доступ запрещён'}, status=403)

    if request.method == 'GET':
        entries = client.contact_history.select_related('manager').order_by('-date')
        return JsonResponse(paginated_response(request, entries, _format_contact_history))

    if request.method == 'POST':
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'detail': 'Invalid JSON'}, status=400)

        result_text = data.get('result', '').strip()
        if not result_text:
            return JsonResponse({'detail': 'Результат обязателен'}, status=400)

        contact_type = data.get('type', 'call')
        valid_types = [t[0] for t in ContactHistory.CONTACT_TYPES]
        if contact_type not in valid_types:
            contact_type = 'call'

        entry = ContactHistory.objects.create(
            client=client,
            manager=user,
            contact_type=contact_type,
            result=result_text,
            date=timezone.now(),
        )
        return JsonResponse({'entry': _format_contact_history(entry)}, status=201)

    return JsonResponse({'detail': 'Method not allowed'}, status=405)


@csrf_exempt
def manager_tasks(request):
    user, is_global, err = _require_manager_scope(request)
    if err:
        return err

    if request.method == 'GET':
        qs = ManagerTask.objects.select_related('client').filter(manager=user)
        status_filter = request.GET.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        return JsonResponse(paginated_response(request, qs, _format_manager_task))

    if request.method == 'POST':
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'detail': 'Invalid JSON'}, status=400)

        text = data.get('text', '').strip()
        if not text:
            return JsonResponse({'detail': 'Текст задачи обязателен'}, status=400)

        client_id = data.get('clientId')
        client = get_object_or_404(ClientProfile, id=client_id)

        deadline = None
        deadline_str = data.get('deadline')
        if deadline_str:
            try:
                deadline = datetime.strptime(deadline_str, '%Y-%m-%d').date()
            except ValueError:
                pass

        task = ManagerTask.objects.create(
            client=client,
            manager=user,
            text=text,
            deadline=deadline,
            comment=data.get('comment', ''),
        )
        return JsonResponse({'task': _format_manager_task(task)}, status=201)

    return JsonResponse({'detail': 'Method not allowed'}, status=405)


@csrf_exempt
@require_http_methods(['POST', 'PUT', 'PATCH'])
def manager_update_task(request, task_id):
    user, is_global, err = _require_manager_scope(request)
    if err:
        return err

    task = get_object_or_404(ManagerTask, id=task_id, manager=user)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'detail': 'Invalid JSON'}, status=400)

    update_fields = []
    if 'status' in data and data['status'] in ('pending', 'completed'):
        task.status = data['status']
        update_fields.append('status')
    if 'comment' in data:
        task.comment = data['comment']
        update_fields.append('comment')
    if 'text' in data:
        task.text = data['text']
        update_fields.append('text')
    if 'deadline' in data:
        try:
            task.deadline = datetime.strptime(data['deadline'], '%Y-%m-%d').date() if data['deadline'] else None
        except ValueError:
            pass
        update_fields.append('deadline')

    if update_fields:
        update_fields.append('updated_at')
        task.save(update_fields=update_fields)

    return JsonResponse({'task': _format_manager_task(task)})


def _normalize_words(text):
    import re
    if not text:
        return set()
    text = text.lower()
    return set(re.findall(r"[a-zа-я0-9]+", text))


def _score_card(query_words, card):
    if not query_words:
        return 0.0
    
    weights = {
        "problem": 1.2,
        "title": 1.0,
        "category": 0.5,
        "solution": 0.3,
    }
    
    score = 0.0
    for field, weight in weights.items():
        field_val = getattr(card, field, "")
        if isinstance(field_val, list):
            field_val = " ".join(field_val)
        
        field_words = _normalize_words(field_val)
        if not field_words:
            continue
            
        common = query_words.intersection(field_words)
        if common:
            # Bonus for matching more query words
            coverage = len(common) / len(query_words)
            score += coverage * weight
            
            # Exact match bonus for short fields
            if len(query_words) == len(field_words) and coverage == 1.0:
                score += 0.5 * weight

    return score


@csrf_exempt
@require_POST
def ai_chat(request):
    user = _current_user(request)
    if not user:
        return JsonResponse({"detail": "Unauthorized"}, status=401)
    
    profile = getattr(user, "profile", None)
    is_expert_user = profile and profile.role in ["admin", "manager", "ai_expert"]
    
    payload = _json(request)
    question = (payload.get("message") or "").strip()
    
    query_words = _normalize_words(question)
    if not query_words:
        return JsonResponse({"answer": "Пожалуйста, введите ваш вопрос.", "suggestEscalation": False})

    # 1. Fetch approved cards
    cards = KnowledgeCard.objects.filter(status="approved")
    
    # 2. Ranking
    ranked = []
    for card in cards:
        score = _score_card(query_words, card)
        if score > 0.1: # Minimum threshold to even consider
            ranked.append((score, card))
    
    ranked.sort(key=lambda x: x[0], reverse=True)
    
    # 3. Decision making
    CONFIDENCE_THRESHOLD = 0.5
    best_score = ranked[0][0] if ranked else 0
    card = ranked[0][1] if ranked else None

    # Guardrails: Forbidden topics if not in KB
    dangerous_keywords = ["пропорции", "смешивание", "гарантия", "совместимость", "срок годности", "разбавление"]
    is_query_dangerous = any(kw in question.lower() for kw in dangerous_keywords)

    if card and best_score >= CONFIDENCE_THRESHOLD:
        solution_lower = card.solution.lower()
        # If user asks for specifics but KB doesn't have them explicitly
        if is_query_dangerous and not any(kw in solution_lower for kw in dangerous_keywords) and not is_expert_user:
            answer = (f"В базе знаний найдена информация по теме '{card.title or card.problem}', но в ней отсутствуют точные технические параметры (пропорции/гарантии). "
                      "Во избежание нарушения технологии, я не могу дать совет. Рекомендую создать обращение к эксперту.")
            return JsonResponse({
                "answer": answer, 
                "sourceId": str(card.id), 
                "suggestEscalation": True
            })
        
        answer = f"На основе утверждённой базы знаний ({card.category}):\n\n{card.solution}"
        if card.restrictions:
            answer += f"\n\nВАЖНО: {card.restrictions}"
        
        return JsonResponse({
            "answer": answer,
            "sourceId": str(card.id),
            "suggestEscalation": False
        })

    # If it's an expert, maybe we give them a "best guess" or allow them to see what the AI would say?
    # For now, if no match, we still suggest escalation for clients, but for experts we might give a hint.
    if is_expert_user:
        return JsonResponse({
            "answer": "В базе знаний точного совпадения не найдено. Как эксперт, вы можете создать новую карточку знаний или ответить на тикет вручную.",
            "suggestEscalation": False
        })

    # 4. Low confidence or No Match for clients
    return JsonResponse({
        "answer": "Недостаточно данных в базе знаний для точного ответа. Перевожу на эксперта. Пожалуйста, создайте тикет с описанием проблемы и фото.",
        "suggestEscalation": True
    })


@csrf_exempt
def admin_managers(request):
    """GET — list all users with manager role (for admin task assignment)."""
    user = _current_user(request)
    if user is None or not (getattr(getattr(user, 'profile', None), 'role', None) in ('admin',) or user.is_staff or user.is_superuser):
        return JsonResponse({'detail': 'Нет доступа'}, status=403)

    if request.method != 'GET':
        return JsonResponse({'detail': 'Method not allowed'}, status=405)

    from .models import Profile
    managers = (
        User.objects.filter(profile__role='manager')
        .select_related('profile')
        .prefetch_related('managed_regions')
        .order_by('username')
    )
    return JsonResponse(paginated_response(
        request,
        managers,
        lambda u: {
            'id': str(u.id),
            'username': u.username,
            'name': u.get_full_name() or u.username,
            'regions': [r.name for r in u.managed_regions.all()],
        },
    ))


@csrf_exempt
def admin_manager_tasks(request):
    """GET — list all manager tasks. POST — create a task for a specific manager."""
    user = _current_user(request)
    if user is None or not (getattr(getattr(user, 'profile', None), 'role', None) in ('admin',) or user.is_staff or user.is_superuser):
        return JsonResponse({'detail': 'Нет доступа'}, status=403)

    if request.method == 'GET':
        qs = ManagerTask.objects.select_related('client', 'manager').order_by('-created_at')
        manager_id = request.GET.get('managerId')
        if manager_id:
            qs = qs.filter(manager_id=manager_id)
        status_filter = request.GET.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        return JsonResponse(paginated_response(request, qs, _format_manager_task))

    if request.method == 'POST':
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'detail': 'Invalid JSON'}, status=400)

        text = data.get('text', '').strip()
        if not text:
            return JsonResponse({'detail': 'Текст задачи обязателен'}, status=400)

        manager_id = data.get('managerId')
        if not manager_id:
            return JsonResponse({'detail': 'Менеджер обязателен'}, status=400)
        try:
            manager = User.objects.get(id=manager_id)
        except User.DoesNotExist:
            return JsonResponse({'detail': 'Менеджер не найден'}, status=404)

        client = None
        client_id = data.get('clientId')
        if client_id:
            client = get_object_or_404(ClientProfile, id=client_id)

        deadline = None
        deadline_str = data.get('deadline')
        if deadline_str:
            try:
                deadline = datetime.strptime(deadline_str, '%Y-%m-%d').date()
            except ValueError:
                pass

        task = ManagerTask.objects.create(
            client=client,
            manager=manager,
            text=text,
            deadline=deadline,
            comment=data.get('comment', ''),
        )
        return JsonResponse({'task': _format_manager_task(task)}, status=201)

    return JsonResponse({'detail': 'Method not allowed'}, status=405)


@csrf_exempt
def admin_manager_task_detail(request, task_id):
    """DELETE — remove a task (admin only)."""
    user = _current_user(request)
    if user is None or not (getattr(getattr(user, 'profile', None), 'role', None) in ('admin',) or user.is_staff or user.is_superuser):
        return JsonResponse({'detail': 'Нет доступа'}, status=403)

    task = get_object_or_404(ManagerTask.objects.select_related('client', 'manager'), id=task_id)

    if request.method == 'DELETE':
        task.delete()
        return JsonResponse({'detail': 'Удалено'})

    return JsonResponse({'detail': 'Method not allowed'}, status=405)


@csrf_exempt
def admin_manager_clients(request, manager_id):
    """GET — list clients accessible to a specific manager."""
    user = _current_user(request)
    if user is None or not (getattr(getattr(user, 'profile', None), 'role', None) in ('admin',) or user.is_staff or user.is_superuser):
        return JsonResponse({'detail': 'Нет доступа'}, status=403)
    if request.method != 'GET':
        return JsonResponse({'detail': 'Method not allowed'}, status=405)
    manager = get_object_or_404(User, id=manager_id)
    is_global = not manager.managed_regions.exists()
    qs = ClientProfile.objects.all().select_related('region', 'distributor')
    qs = _filter_by_manager_scope(manager, is_global, qs)
    return JsonResponse(paginated_response(
        request,
        qs,
        lambda c: {'id': str(c.id), 'name': c.company_name},
    ))


def _auto_create_ticket(client, question, category):
    # Check for existing open ticket with same question to avoid spam
    exists = ExpertTicket.objects.filter(
        client=client, 
        question=question, 
        status__in=["open", "escalated"]
    ).exists()
    
    if not exists:
        ExpertTicket.objects.create(
            client=client,
            question=question,
            category=category,
            status="open",
            risk="medium"
        )
