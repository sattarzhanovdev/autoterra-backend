"""Single-use bearer capabilities; raw tokens only leave this module for email/forms."""
import hashlib
import re
import secrets
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from api.models import OrderEmailActionToken
from api.services import orders


class EmailActionError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


def token_hash(raw):
    if not isinstance(raw, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', raw):
        raise EmailActionError('Ссылка недействительна')
    return hashlib.sha256(raw.encode('ascii')).hexdigest()


@transaction.atomic
def issue_confirm_token(order):
    # Same order-first lock order as consumption and mobile confirmation.
    orders.locked_order(order.pk)
    now = timezone.now()
    OrderEmailActionToken.objects.filter(order=order, action='confirm', used_at__isnull=True).update(used_at=now)
    raw = secrets.token_urlsafe(32)  # 256 bits of entropy
    OrderEmailActionToken.objects.create(order=order, action='confirm',
        token_hash=token_hash(raw), expires_at=now + timedelta(hours=24))
    return raw


def confirmation_url(raw):
    base = settings.ORDER_EMAIL_ACTION_BASE_URL.rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.query or parsed.fragment or parsed.username or parsed.path:
        raise ImproperlyConfigured('ORDER_EMAIL_ACTION_BASE_URL must be an HTTPS origin')
    return base + reverse('order-email-confirm') + '?' + urlencode({'token': raw})


def _validate(token):
    if token is None:
        raise EmailActionError('Ссылка недействительна')
    if token.used_at:
        raise EmailActionError('Заказ уже был обработан по этой ссылке', 409)
    if token.expires_at <= timezone.now():
        raise EmailActionError('Срок действия ссылки истёк', 410)


def preview(raw):
    token = OrderEmailActionToken.objects.select_related('order__client').filter(
        token_hash=token_hash(raw), action='confirm').first()
    _validate(token)
    try:
        orders.validate_confirmation(token.order, source='email')
    except orders.OrderError as exc:
        raise EmailActionError(f'Заказ уже имеет статус: {token.order.get_status_display()}. {exc.detail}', 409) from None
    return token.order


@transaction.atomic
def confirm(raw):
    digest = token_hash(raw)
    token = OrderEmailActionToken.objects.filter(token_hash=digest, action='confirm').first()
    _validate(token)
    order = orders.locked_order(token.order_id)
    token = OrderEmailActionToken.objects.select_for_update().get(pk=token.pk)
    _validate(token)  # Check again after waiting for the order lock.
    try:
        order = orders.transition_order(order.pk, 'confirmed', source='email')
    except orders.OrderError as exc:
        raise EmailActionError(f'Заказ уже имеет статус: {order.get_status_display()}. {exc.detail}', 409) from None
    token.used_at = timezone.now()
    token.save(update_fields=['used_at'])
    return order
