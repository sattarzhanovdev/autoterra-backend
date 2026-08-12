"""Бонусный счёт клиента.

Реферальный бонус капает сюда автоматически процентом от закупок
приглашённого — начислением занимается ``api.services.referral_bonus``.
При оплате заказа бонус уменьшает сумму, которая уходит в ЮKassa.

Баланс нигде не хранится отдельным числом — он всегда сумма операций. Так
любое расхождение видно по реестру, а не остаётся догадкой.

Все изменения идут через ``select_for_update``: два параллельных запроса на
оплату не должны списать один и тот же бонус дважды.
"""

import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")


def balance(client) -> Decimal:
    """Текущий бонусный баланс клиента."""
    from api.models import BonusTransaction

    total = BonusTransaction.objects.filter(client=client).aggregate(
        total=Sum("amount")
    )["total"]
    return Decimal(total or 0).quantize(Decimal("0.01"))


def spendable_for_order(client, order_total: Decimal) -> Decimal:
    """Сколько бонусов реально можно списать в этот заказ."""
    available = balance(client)
    if available <= ZERO or order_total <= ZERO:
        return ZERO
    return min(available, Decimal(order_total)).quantize(Decimal("0.01"))


def debit_for_order(client, order, requested: Decimal):
    """Списать бонусы в счёт заказа. Возвращает фактически списанную сумму.

    Списываем не больше баланса и не больше суммы заказа — переплатить
    бонусами нельзя, остаток должен оставаться на счёте.
    """
    from api.models import BonusTransaction, ClientProfile

    requested = Decimal(requested or 0)
    if requested <= ZERO:
        return ZERO

    with transaction.atomic():
        # Блокируем клиента, а не транзакции: иначе два запроса на оплату
        # прочитают один и тот же баланс и спишут бонус дважды.
        ClientProfile.objects.select_for_update().get(pk=client.pk)

        already = BonusTransaction.objects.filter(order=order, kind="order").aggregate(
            total=Sum("amount")
        )["total"]
        if already:
            # По заказу уже списывали — повторно не трогаем.
            return abs(Decimal(already)).quantize(Decimal("0.01"))

        applied = min(requested, spendable_for_order(client, order.total_amount))
        if applied <= ZERO:
            return ZERO

        BonusTransaction.objects.create(
            client=client,
            amount=-applied,
            kind="order",
            order=order,
            comment=f"Оплата заказа ORD-{order.id:05d}",
        )
        return applied


def refund_for_order(order):
    """Вернуть бонусы, если оплата не состоялась.

    Без этого отменённый платёж съедал бы бонус: деньги за заказ не пришли, а
    со счёта уже списано.
    """
    from api.models import BonusTransaction

    with transaction.atomic():
        spent = BonusTransaction.objects.filter(order=order, kind="order").aggregate(
            total=Sum("amount")
        )["total"]
        spent = abs(Decimal(spent or 0))
        if spent <= ZERO:
            return ZERO

        returned = BonusTransaction.objects.filter(order=order, kind="refund").aggregate(
            total=Sum("amount")
        )["total"]
        if Decimal(returned or 0) >= spent:
            return ZERO  # уже возвращали

        amount = spent - Decimal(returned or 0)
        BonusTransaction.objects.create(
            client=order.client,
            amount=amount,
            kind="refund",
            order=order,
            comment=f"Возврат по отменённой оплате ORD-{order.id:05d}",
        )
        logger.info("Бонус возвращён по заказу %s: %s", order.pk, amount)
        return amount
