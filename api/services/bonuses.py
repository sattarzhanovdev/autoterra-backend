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
from decimal import Decimal, ROUND_DOWN

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
    return min(available, (Decimal(order_total) * Decimal("0.10")).quantize(Decimal("0.01"), rounding=ROUND_DOWN)).quantize(Decimal("0.01"))


def debit_for_order(client, order, requested: Decimal):
    """Списать бонусы в счёт заказа. Возвращает фактически списанную сумму.

    Списываем не больше баланса и 10% итоговой суммы заказа после скидок.
    Остаток остаётся на бонусном счёте.
    """
    from api.models import BonusTransaction, ClientProfile

    requested = Decimal(requested or 0)
    if not requested.is_finite() or requested < ZERO:
        raise ValueError("Некорректная сумма бонусов")
    requested = requested.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if requested <= ZERO:
        return ZERO

    with transaction.atomic():
        # Блокируем клиента, а не транзакции: иначе два запроса на оплату
        # прочитают один и тот же баланс и спишут бонус дважды.
        ClientProfile.objects.select_for_update().get(pk=client.pk)

        already = BonusTransaction.objects.filter(order=order, kind__in=["order", "refund"]).aggregate(
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
    from api.models import BonusTransaction, ClientProfile

    with transaction.atomic():
        # Serialize refunds with debits and other refunds on the same account.
        ClientProfile.objects.select_for_update().get(pk=order.client_id)
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


def quote(client, total, requested=ZERO):
    requested = Decimal(str(requested))
    if not requested.is_finite() or requested < ZERO or requested > Decimal("9999999999.99") or requested.as_tuple().exponent < -2:
        raise ValueError("Некорректная сумма бонусов")
    maximum = spendable_for_order(client, total)
    applied = min(requested, maximum)
    return {"balance": str(balance(client)), "maxAllowed": str(maximum),
            "requested": str(requested), "applied": str(applied),
            "payableTotal": str((Decimal(total) - applied).quantize(Decimal("0.01")))}


def manual_debit(client, amount, reason, actor):
    from api.models import BonusTransaction, ClientProfile, AuditLog
    if not actor.is_active or not actor.is_staff or not actor.has_perm("api.add_bonustransaction"):
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied
    amount = Decimal(str(amount))
    reason = reason.strip()
    if not amount.is_finite() or amount <= ZERO or amount.as_tuple().exponent < -2 or not reason or len(reason) > 255:
        raise ValueError("Укажите положительную сумму и причину (до 255 символов)")
    with transaction.atomic():
        ClientProfile.objects.select_for_update().get(pk=client.pk)
        if amount > balance(client):
            raise ValueError("Нельзя списать больше текущего баланса")
        entry = BonusTransaction.objects.create(client=client, amount=-amount, kind="manual", comment=reason)
        AuditLog.objects.create(user=actor, action="Manual bonus debit", model_name="BonusTransaction", object_id=str(entry.pk), changes={"amount": str(-amount), "reason": reason, "clientId": client.pk})
        return entry
