"""Single-use bearer capabilities; raw tokens only leave this module for email/forms."""
import hashlib
import json
import re
import secrets
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from api.models import ClientProfile, OrderEmailActionToken, Product
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


def order_revision(order):
    """A form rendered before a concurrent item/price edit must be reopened."""
    snapshot = {
        'status': order.status, 'client': order.client_id, 'distributor': order.distributor_id,
        'payment': order.payment_method, 'delivery': order.delivery_method,
        'store': order.store_id,
        'items': list(order.items.order_by('pk').values_list('pk', 'product_id', 'quantity', 'price')),
    }
    return hashlib.sha256(json.dumps(snapshot, default=str, sort_keys=True).encode()).hexdigest()


def editor_context(order, *, submitted=None):
    items = list(order.items.select_related('product').order_by('pk'))
    for item in items:
        product = item.product
        item.available = (None if product.status == 'onOrder'
                          else product.quantity + item.reserved_quantity)
        item.entered_quantity = (submitted.get(f'quantity_{item.pk}', item.quantity)
                                 if submitted is not None else item.quantity)
    return {'order': order, 'items': items, 'item_count': len(items),
            'revision': order_revision(order)}


def _email_quantities(order, quantities):
    if not isinstance(quantities, dict):
        raise EmailActionError('Укажите количество для каждой позиции заказа.')
    items = list(order.items.order_by('pk'))
    if set(quantities) != {str(item.pk) for item in items}:
        raise EmailActionError('Состав заказа изменился. Откройте ссылку из письма заново.', 409)
    rows = []
    for item in items:
        try:
            quantity = orders._quantity(quantities[str(item.pk)])
        except orders.OrderError:
            raise EmailActionError('Количество должно быть целым неотрицательным числом.') from None
        if quantity > item.quantity:
            raise EmailActionError('Из письма можно только уменьшить количество. Добавьте товары в AutoTerra.')
        rows.append({'itemId': item.pk, 'quantity': quantity})
    if not any(row['quantity'] for row in rows):
        raise EmailActionError('Нельзя убрать все позиции. Для отклонения заказа откройте AutoTerra.')
    return rows, any(row['quantity'] != item.quantity for row, item in zip(rows, items))


def _validate_available(order, rows):
    items = list(order.items.order_by('pk'))
    products = {p.pk: p for p in Product.objects.select_for_update().filter(
        pk__in=[item.product_id for item in items]).order_by('pk')}
    requested, reserved = {}, {}
    for row, item in zip(rows, items):
        requested[item.product_id] = requested.get(item.product_id, 0) + row['quantity']
        reserved[item.product_id] = reserved.get(item.product_id, 0) + item.reserved_quantity
    for product_id, quantity in requested.items():
        product = products.get(product_id)
        if quantity and (product is None or not product.is_active or product.distributor_id != order.distributor_id):
            raise EmailActionError('Одна из позиций больше недоступна. Укажите для неё 0 или откройте AutoTerra.')
        if product is not None and product.status != 'onOrder':
            available = product.quantity + reserved[product_id]
            if quantity > available:
                raise EmailActionError(f'«{product.name}»: доступно {available} шт. Уменьшите количество.')


def _reserve_email_items(order):
    """Legacy/partially reserved lines must not leave confirmed stock free."""
    from .stock import reserve_product
    items = list(order.items.order_by('pk'))
    products = {p.pk: p for p in Product.objects.select_for_update().filter(
        pk__in=[item.product_id for item in items]).order_by('pk')}
    for item in items:
        product = products[item.product_id]
        missing = item.quantity - item.reserved_quantity
        if missing > 0 and product.status != 'onOrder':
            item.reserved_quantity += reserve_product(product, missing)
            item.save(update_fields=['reserved_quantity'])


@transaction.atomic
def confirm(raw, *, quantities=None, revision=None, reason=''):
    digest = token_hash(raw)
    token = OrderEmailActionToken.objects.filter(token_hash=digest, action='confirm').first()
    _validate(token)
    order = orders.locked_order(token.order_id)
    token = OrderEmailActionToken.objects.select_for_update().get(pk=token.pk)
    _validate(token)  # Check again after waiting for the order lock.
    try:
        orders.validate_confirmation(order, source='email')
    except orders.OrderError as exc:
        raise EmailActionError(f'Заказ уже имеет статус: {order.get_status_display()}. {exc.detail}', 409) from None
    if quantities is not None:
        if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{64}', revision) or not secrets.compare_digest(revision, order_revision(order)):
            raise EmailActionError('Состав заказа изменился. Откройте ссылку из письма заново.', 409)
        rows, changed = _email_quantities(order, quantities)
    else:
        # Keep old confirmation forms compatible without silently changing items.
        rows = [{'itemId': item.pk, 'quantity': item.quantity} for item in order.items.order_by('pk')]
        changed = False
    if not isinstance(reason, str) or len(reason) > 1000:
        raise EmailActionError('Комментарий должен быть не длиннее 1000 символов.')
    try:
        # Match shared adjustment lock order: order -> client -> products.
        order.client = ClientProfile.objects.select_for_update(of=('self',)).get(pk=order.client_id)
        _validate_available(order, rows)
        if changed:
            order = orders.adjust_order(order.pk, {'items': rows,
                'reason': reason.strip() or 'Наличие уточнено оператором из письма'}, source='email')
            _reserve_email_items(order)
        else:
            _reserve_email_items(order)
            order = orders.transition_order(order.pk, 'confirmed', source='email')
    except orders.OrderError as exc:
        raise EmailActionError(exc.detail, exc.status_code) from None
    token.used_at = timezone.now()
    token.save(update_fields=['used_at'])
    return order
