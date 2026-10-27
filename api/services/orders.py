"""Locked order mutations shared by HTTP endpoints and Django Admin.

Callers enforce their access scope; this service enforces order, payment,
stock and relationship invariants. OrderItem is never edited directly by Admin.
"""
from datetime import date, datetime, time

from django.db import transaction
from django.utils import timezone

from api.models import AuditLog, ClientProfile, Order, OrderAdjustment, OrderItem, Product, Store, Distributor
from api.serializers import coerce_decimal
from api.services.pricing import price_for_client
from api.services.order_discounts import quote_order
from api.services.stock import reserve_product, release_product, restore_order_stock

EDITABLE_STATUSES = {'new', 'confirmed', 'adjusted', 'accepted'}
ACTIVE_PAYMENT_STATUSES = ('pending', 'waiting_for_capture')
METADATA_FIELDS = {
    'client', 'store', 'distributor', 'delivery_method', 'courier',
    'estimated_delivery_date', 'comment', 'rejection_reason', 'payment_method',
}
PENDING_MESSAGE = 'Дождитесь завершения или отмены платежа в ЮKassa, затем обновите заказ.'


class OrderError(Exception):
    def __init__(self, detail, status_code=400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def locked_order(order_id):
    order = Order.objects.select_for_update(of=('self',)).select_related(
        'client', 'client__user', 'distributor', 'store').filter(pk=order_id).first()
    if order is None:
        raise OrderError('Заказ не найден', 404)
    return order


def has_active_payment(order):
    return order.payments.filter(status__in=ACTIVE_PAYMENT_STATUSES).exists()


def is_editable_order(order):
    return bool(order and order.status in EDITABLE_STATUSES and not order.paid_at
                and not order.stock_restored and not order.payments.filter(
                    status__in=(*ACTIVE_PAYMENT_STATUSES, 'succeeded')).exists())


def ensure_editable_order(order):
    if has_active_payment(order):
        raise OrderError(PENDING_MESSAGE, 409)
    if not is_editable_order(order):
        raise OrderError('Редактирование оплаченного или закрытого заказа недоступно')


def items_snapshot(order):
    # Never use a prefetched related-manager cache for history or totals.
    return [{
        'productId': str(item.product_id), 'sku': item.sku, 'name': item.name,
        'price': float(item.price), 'quantity': item.quantity, 'total': float(item.total),
    } for item in OrderItem.objects.filter(order=order).order_by('id')]


def _audit(order, actor, action, changes):
    AuditLog.objects.create(user=actor, action=action, model_name='Order',
                            object_id=str(order.pk), changes=changes)


def _notify_event(order, old_status, event, reason=''):
    # Retain existing notification/email implementation and its delivery/error
    # handling. Lazy import avoids a views/service import cycle.
    from api import views
    if event == 'adjust':
        views._notify_client_order(order, 'Заказ скорректирован',
            f'Заказ ORD-{order.id:05d} изменён оператором. Новая сумма: {order.total_amount} ₽. Подтвердите или отмените.')
        recipients = views._client_email_list(order)
    elif event == 'confirm':
        views._notify_client_order(order, 'Заказ подтверждён',
            f'Заказ ORD-{order.id:05d} подтверждён. Сумма: {order.total_amount} ₽. ' + (
                'Оплата наличными при доставке. Заказ передан в сборку.' if order.payment_method == 'cash' else 'Можно оплатить.'))
        recipients = views._client_email_list(order)
    elif event == 'accept':
        views._notify_operator_order(order, 'Клиент принял корректировку',
            f'{order.client.company_name} принял корректировку заказа ORD-{order.id:05d}')
        recipients = None
    elif event == 'cancel':
        views._notify_operator_order(order, 'Заказ отменён',
            f'{order.client.company_name} отменил заказ ORD-{order.id:05d}')
        recipients = None
    elif event == 'reject':
        views._notify_client_order(order, 'Заказ отклонён',
            f'Заказ ORD-{order.id:05d} отклонён. Причина: {order.rejection_reason}')
        recipients = views._client_email_list(order)
    elif event == 'ship':
        views._notify_client_order(order, 'Заказ отправлен',
            f'Заказ ORD-{order.id:05d} отправлен. Ожидайте доставку.')
        recipients = views._client_email_list(order)
    else:
        views._notify_client_order(order, 'Статус заказа',
            f'Заказ ORD-{order.pk:05d}: {order.get_status_display()}')
        recipients = None
    views._send_order_status_email(order, old_status, order.status, recipients=recipients)


def _sync_delivery(order, actor=None, invalidate_picks=False):
    """Keep existing cash-delivery tasks consistent with changed order data."""
    task = order.courier_tasks.select_for_update().first()
    if task is None or task.status in ('in_progress', 'delivered', 'returned'):
        return
    ready = order.delivery_method == 'courier' and (
        order.status in ('paid', 'shipped', 'fulfilled') or
        (order.status == 'confirmed' and order.payment_method == 'cash'))
    history = list(task.status_history or [])
    if not ready and task.status in ('created', 'assigned'):
        task.status = 'cancelled'
        history.append({'status': 'cancelled', 'at': timezone.now().isoformat(),
                        'by': str(actor.pk) if actor else None,
                        'comment': 'Приостановлено из-за изменения заказа', 'order_paused': True})
    elif ready and task.status == 'cancelled' and history and history[-1].get('order_paused'):
        task.status = 'assigned' if order.courier_id else 'created'
        history.append({'status': task.status, 'at': timezone.now().isoformat(),
                        'by': str(actor.pk) if actor else None,
                        'comment': 'Заказ снова готов к доставке', 'order_paused': False})
    task.status_history = history
    task.client = order.client
    task.courier = order.courier
    task.address = order.store.address if order.store_id else order.client.city
    task.contact_name = order.client.contact_name
    task.contact_phone = order.client.phone
    task.scheduled_time = (timezone.make_aware(datetime.combine(order.estimated_delivery_date, time(10)))
                           if order.estimated_delivery_date else None)
    task.save(update_fields=['client', 'courier', 'address', 'contact_name', 'contact_phone',
                             'scheduled_time', 'status', 'status_history'])
    if invalidate_picks:
        task.picked_items.all().delete()


def _quantity(value, *, positive=False):
    try:
        if isinstance(value, bool) or str(value) != str(int(value)):
            raise ValueError
        number = int(value)
        if not (1 if positive else 0) <= number <= 1000000:
            raise ValueError
        return number
    except (TypeError, ValueError, OverflowError):
        raise OrderError('Некорректные данные позиции') from None


def _adjustment_maps(payload):
    if not isinstance(payload, dict):
        raise OrderError('Некорректные данные корректировки')
    updates, additions = payload.get('items') or [], payload.get('newItems') or []
    if (not isinstance(updates, list) or not isinstance(additions, list)
            or any(not isinstance(row, dict) for row in updates + additions)
            or not isinstance(payload.get('reason', ''), str)):
        raise OrderError('Некорректные данные корректировки')
    if not updates and not additions:
        raise OrderError('Передайте позиции для корректировки')
    quantities, new_quantities = {}, {}
    for row in updates:
        try:
            quantities[int(row.get('itemId'))] = _quantity(row.get('quantity'))
        except (TypeError, ValueError, OverflowError):
            raise OrderError('Некорректные данные позиции') from None
    for row in additions:
        try:
            product_id = int(row.get('productId'))
        except (TypeError, ValueError, OverflowError):
            raise OrderError('Некорректные данные новой позиции') from None
        quantity = _quantity(row.get('quantity'), positive=True)
        new_quantities[product_id] = new_quantities.get(product_id, 0) + quantity
        if new_quantities[product_id] > 1000000:
            raise OrderError('Слишком большое количество')
    return quantities, new_quantities, payload.get('reason', '').strip()


def _reprice_items(order, products):
    items = list(OrderItem.objects.filter(order=order))
    quantities = {}
    for item in items:
        quantities[item.product_id] = quantities.get(item.product_id, 0) + item.quantity
    prices, _ = quote_order(order.client, products, quantities)
    for item in items:
        item.price = prices[item.product_id]
        item.save(update_fields=['price'])


def _valid_total(order):
    total = coerce_decimal(sum(item.total for item in OrderItem.objects.filter(order=order)), 12, 2)
    if total is None or total < 0:
        raise OrderError('Некорректная сумма заказа')
    return total


def _record_adjustment(order, original, actor, reason, old_status, source):
    order.status = 'adjusted'
    order.confirmed_at = None
    order.save(update_fields=['status', 'confirmed_at'])
    OrderAdjustment.objects.create(order=order, original_items=original,
        adjusted_items=items_snapshot(order), reason=reason, created_by=actor)
    _sync_delivery(order, actor, invalidate_picks=True)
    _audit(order, actor, f'Order adjust: {old_status} -> adjusted', {'reason': reason, 'source': source})
    order.refresh_from_db()
    _notify_event(order, old_status, 'adjust', reason)


@transaction.atomic
def adjust_order(order_id, payload, *, actor=None, source='api'):
    order = locked_order(order_id)
    ensure_editable_order(order)
    order.client = ClientProfile.objects.select_for_update(of=('self',)).get(pk=order.client_id)
    quantities, additions, reason = _adjustment_maps(payload)
    item_ids = set(order.items.values_list('pk', flat=True))
    if set(quantities) - item_ids:
        raise OrderError('Позиция не принадлежит заказу')
    product_ids = set(order.items.values_list('product_id', flat=True)) | set(additions)
    products = {p.pk: p for p in Product.objects.select_for_update(of=('self',)).filter(
        pk__in=product_ids).order_by('pk')}
    original = items_snapshot(order)
    for item in order.items.all():
        if item.pk not in quantities:
            continue
        quantity = quantities[item.pk]
        delta = quantity - item.quantity
        product = products[item.product_id]
        if product.distributor_id != order.distributor_id:
            raise OrderError('Товар недоступен у этого дистрибьютора')
        if delta > 0:
            if product.status != 'onOrder' and product.quantity < delta:
                raise OrderError(f'Недостаточно «{product.name}» на складе. Доступно: {product.quantity}')
            item.reserved_quantity += reserve_product(product, delta)
        elif delta < 0:
            reserved = min(item.reserved_quantity, quantity)
            released = item.reserved_quantity - reserved
            if released:
                release_product(product, released)
            item.reserved_quantity = reserved
        if quantity == 0:
            item.delete()
        elif quantity != item.quantity:
            item.quantity = quantity
            item.save(update_fields=['quantity', 'reserved_quantity'])
    for product_id, quantity in additions.items():
        product = products.get(product_id)
        if product is None or not product.is_active or product.distributor_id != order.distributor_id:
            raise OrderError('Товар недоступен у этого дистрибьютора')
        if product.status == 'outOfStock' or (product.status != 'onOrder' and product.quantity < quantity):
            raise OrderError(f'Недостаточно «{product.name}» на складе. Доступно: {product.quantity}')
        reserved = reserve_product(product, quantity)
        existing = order.items.filter(product_id=product_id).first()
        if existing:
            existing.quantity += quantity
            if existing.quantity > 1000000:
                raise OrderError('Слишком большое количество')
            existing.reserved_quantity += reserved
            existing.save(update_fields=['quantity', 'reserved_quantity'])
        else:
            OrderItem.objects.create(order=order, product=product, sku=product.sku, name=product.name,
                category=product.category, brand=product.brand, volume=product.volume,
                price=price_for_client(order.client, product), quantity=quantity, reserved_quantity=reserved)
    if not order.items.exists():
        raise OrderError('После корректировки в заказе не осталось позиций. Отклоните заказ.')
    _reprice_items(order, products)
    _valid_total(order)
    if items_snapshot(order) != original:
        _record_adjustment(order, original, actor, reason, order.status, source)
    order.refresh_from_db()
    return order


def validate_metadata(order, changes):
    ensure_editable_order(order)
    if set(changes) - METADATA_FIELDS:
        raise OrderError('Системные поля заказа нельзя изменять напрямую')
    changed = {key: value for key, value in changes.items() if getattr(order, key) != value}
    if not changed:
        return changed
    if 'distributor' in changed and order.status != 'new':
        raise OrderError('Дистрибьютора можно менять только у нового неоплаченного заказа')
    if set(changed) & {'client', 'distributor', 'payment_method'} and order.payments.exists():
        raise OrderError('После создания платежа нельзя менять клиента, дистрибьютора или способ оплаты', 409)
    client = changes.get('client', order.client)
    distributor = changes.get('distributor', order.distributor)
    store = changes.get('store', order.store)
    courier = changes.get('courier', order.courier)
    delivery = changes.get('delivery_method', order.delivery_method)
    payment = changes.get('payment_method', order.payment_method)
    if not client or not distributor or client.distributor_id != distributor.pk:
        raise OrderError('Клиент должен принадлежать дистрибьютору заказа')
    if 'client' in changed and (client.status != 'active' or (client.user_id and not client.user.is_active)):
        raise OrderError('Выберите активного клиента')
    if not distributor.is_active:
        raise OrderError('Дистрибьютор недоступен')
    if store and (not store.is_active or store.client_id != client.pk):
        raise OrderError('Магазин должен быть активным и принадлежать выбранному клиенту')
    if courier and (not courier.is_active or getattr(getattr(courier, 'profile', None), 'role', None) != 'courier'):
        raise OrderError('Выберите активного курьера')
    if delivery not in dict(Order.DELIVERY_CHOICES):
        raise OrderError('Некорректный способ доставки')
    if delivery == 'self_pickup' and courier:
        raise OrderError('Для самовывоза уберите назначенного курьера')
    if payment not in ('online', 'cash'):
        raise OrderError('Некорректный способ оплаты')
    if payment == 'cash' and (not client.cash_payment_allowed or delivery != 'courier' or order.total_amount <= 0):
        raise OrderError('Наличные доступны только разрешённому клиенту при курьерской доставке')
    if 'distributor' in changed and order.items.exclude(product__distributor=distributor).exists():
        raise OrderError('Состав заказа несовместим с выбранным дистрибьютором')
    if any(not isinstance(changes.get(field, ''), str) for field in ('comment', 'rejection_reason')):
        raise OrderError('Комментарий и причина должны быть текстом')
    estimated = changes.get('estimated_delivery_date')
    if estimated is not None and not isinstance(estimated, date):
        raise OrderError('Некорректная дата доставки')
    return changed


@transaction.atomic
def update_metadata(order_id, changes, *, actor=None, reason='Изменение заказа через Django Admin'):
    order = locked_order(order_id)
    # Reload/lock related records: do not trust ModelForm's earlier instances.
    changes = dict(changes)
    client_ids = {order.client_id}
    if changes.get('client'):
        client_ids.add(changes['client'].pk)
    clients = {c.pk: c for c in ClientProfile.objects.select_for_update(of=('self',)).filter(
        pk__in=client_ids).select_related('user').order_by('pk')}
    order.client = clients[order.client_id]
    if 'client' in changes:
        changes['client'] = clients.get(changes['client'].pk) if changes['client'] else None
    if changes.get('distributor'):
        changes['distributor'] = Distributor.objects.select_for_update().get(pk=changes['distributor'].pk)
    if changes.get('store'):
        changes['store'] = Store.objects.select_for_update().filter(pk=changes['store'].pk).first()
        if changes['store'] is None:
            raise OrderError('Магазин не найден')
    changed = validate_metadata(order, changes)
    if not changed:
        return order
    original, old_status = items_snapshot(order), order.status
    for key, value in changed.items():
        setattr(order, key, value)
    order.save(update_fields=list(changed))
    if 'client' in changed and order.items.exists():
        items = list(order.items.all())
        products = {p.pk: p for p in Product.objects.select_for_update(of=('self',)).filter(
            pk__in=[item.product_id for item in items]).order_by('pk')}
        _reprice_items(order, products)
        _valid_total(order)
        _record_adjustment(order, original, actor, reason, old_status, 'admin')
    else:
        _sync_delivery(order, actor, invalidate_picks=bool(set(changed) & {'courier', 'payment_method', 'delivery_method'}))
    _audit(order, actor, 'Order metadata changed', {
        key: (value.pk if hasattr(value, 'pk') else value.isoformat() if isinstance(value, date) else value)
        for key, value in changed.items()})
    order.refresh_from_db()
    return order


def validate_confirmation(order, *, source='operator'):
    if has_active_payment(order):
        raise OrderError(PENDING_MESSAGE, 409)
    if order.paid_at or order.payments.filter(status='succeeded').exists():
        raise OrderError('Оплаченный заказ нельзя изменить или отменить')
    if source == 'client':
        if order.status != 'adjusted':
            raise OrderError('Заказ не в статусе корректировки')
    elif order.status not in ('new', 'accepted'):
        raise OrderError('Изменения должен принять клиент')
    if not order.can_transition_to('confirmed'):
        raise OrderError('Недопустимый переход статуса заказа')


@transaction.atomic
def transition_order(order_id, target, *, actor=None, source='operator', reason='', delivery_updates=None):
    order = locked_order(order_id)
    if has_active_payment(order):
        raise OrderError(PENDING_MESSAGE, 409)
    if target not in ('confirmed', 'rejected', 'cancelled', 'shipped', 'fulfilled'):
        raise OrderError('Недопустимый переход статуса заказа')
    if target == 'confirmed':
        validate_confirmation(order, source=source)
    if target in ('confirmed', 'rejected', 'cancelled') and (order.paid_at or order.payments.filter(status='succeeded').exists()):
        raise OrderError('Оплаченный заказ нельзя изменить или отменить')
    if not order.can_transition_to(target):
        raise OrderError('Недопустимый переход статуса заказа')
    if not isinstance(reason, str):
        raise OrderError('Причина должна быть текстом')
    delivery_updates = delivery_updates or {}
    if set(delivery_updates) - {'courier', 'estimated_delivery_date'}:
        raise OrderError('Недопустимые поля перехода статуса')
    old_status = order.status
    order.status = target
    fields = ['status', *delivery_updates]
    for field, value in delivery_updates.items():
        setattr(order, field, value)
    if target == 'confirmed':
        order.confirmed_at = timezone.now()
        fields.append('confirmed_at')
    elif target == 'shipped':
        order.shipped_at = timezone.now()
        fields.append('shipped_at')
    elif target in ('rejected', 'cancelled'):
        restore_order_stock(order)
        order.rejection_reason = reason or ('Отменено клиентом' if source == 'client' else 'Не указана')
        fields.append('rejection_reason')
    order.save(update_fields=fields)
    _sync_delivery(order, actor)
    event = {'confirmed': 'accept' if source == 'client' else 'confirm',
             'rejected': 'reject', 'cancelled': 'cancel', 'shipped': 'ship', 'fulfilled': 'fulfill'}[target]
    _audit(order, actor, 'Order confirmed from email' if source == 'email' and target == 'confirmed'
           else f'Order {event}: {old_status} -> {target}',
           {'reason': reason, 'source': source, **({'orderId': order.pk} if source == 'email' else {})})
    _notify_event(order, old_status, event, reason)
    return order
