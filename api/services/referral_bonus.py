"""Начисление реферального бонуса процентом от закупок приглашённого.

Прежняя схема была плоской: приглашённый набрал 30 000 ₽ — пригласивший получил
сертификат на 5 000 ₽. Это 16,7% от оборота, что заметно выше маржи, поэтому
её заменили ставкой по ступеням.

Как считается
─────────────
Ступень определяется накопленным оборотом **каждого приглашённого СТО по
отдельности**, а ставка применяется ко всему его обороту, а не к остатку сверх
ступени. Приглашённый закупил на 300 000 ₽ → ступень 200 000 (1,5%) → бонус
4 500 ₽.

Начисляем разницей: при переходе на следующую ступень ставка растёт задним
числом на весь оборот, и пригласившему доначисляется только то, чего не
хватает до новой суммы. Так повторный вызов ничего не задваивает — сколько
уже начислено, видно по реестру BonusTransaction.

Сгорание
────────
Пригласивший должен закупаться сам: если за календарный месяц его собственные
подтверждённые закупки не дотянули до REFERRAL_ACTIVITY_MIN, весь накопленный
бонусный баланс сгорает. Сгорание — такая же операция в реестре, поэтому по
счёту всегда видно, что и когда списали.
"""

import logging
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Sum

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")
CENTS = Decimal("0.01")

# (оборот приглашённого от, ставка в процентах). Ставка применяется ко всему
# обороту. Настраивается через REFERRAL_BONUS_TIERS.
DEFAULT_TIERS = (
    (Decimal(0), Decimal("0.5")),
    (Decimal(100000), Decimal("1.0")),
    (Decimal(200000), Decimal("1.5")),
    (Decimal(400000), Decimal("2.0")),
    (Decimal(800000), Decimal("2.5")),
)

# Сколько пригласивший должен закупать сам за календарный месяц, чтобы бонусы
# не сгорели.
DEFAULT_ACTIVITY_MIN = Decimal(10000)


def tiers() -> tuple[tuple[Decimal, Decimal], ...]:
    raw = getattr(settings, "REFERRAL_BONUS_TIERS", None)
    if not raw:
        return DEFAULT_TIERS
    parsed = tuple(
        (Decimal(str(threshold)), Decimal(str(rate))) for threshold, rate in raw
    )
    return tuple(sorted(parsed, key=lambda item: item[0]))


def activity_min() -> Decimal:
    return Decimal(str(getattr(settings, "REFERRAL_ACTIVITY_MIN", DEFAULT_ACTIVITY_MIN)))


def rate_for(volume) -> Decimal:
    """Ставка в процентах для накопленного оборота приглашённого."""
    volume = Decimal(volume or 0)
    rate = ZERO
    for threshold, tier_rate in tiers():
        if volume >= threshold:
            rate = tier_rate
        else:
            break
    return rate


def earned_for(volume) -> Decimal:
    """Сколько всего причитается пригласившему за такой оборот."""
    volume = Decimal(volume or 0)
    if volume <= ZERO:
        return ZERO
    return (volume * rate_for(volume) / Decimal(100)).quantize(CENTS, rounding=ROUND_HALF_UP)


def next_tier(volume) -> tuple[Decimal, Decimal] | None:
    """Следующая ступень и сколько до неё осталось. None — ступень последняя."""
    volume = Decimal(volume or 0)
    for threshold, tier_rate in tiers():
        if volume < threshold:
            return threshold - volume, tier_rate
    return None


def accrued_for(referral) -> Decimal:
    """Сколько уже начислено по этой связке. Сгоревшее тоже считается начисленным.

    Иначе после сгорания следующая же закупка приглашённого начислила бы всё
    заново, и сгорание не значило бы ничего.
    """
    from api.models import BonusTransaction

    total = BonusTransaction.objects.filter(
        referral=referral, kind="referral"
    ).aggregate(total=Sum("amount"))["total"]
    return Decimal(total or 0).quantize(CENTS)


def accrue(referral) -> Decimal:
    """Доначислить пригласившему то, чего не хватает до текущей ступени.

    Возвращает начисленную сумму. Повторный вызов без новых закупок вернёт 0.
    """
    from api.models import BonusTransaction, ClientProfile

    if not referral.counts_toward_bonus:
        # Заявку ещё не подтвердил приглашённый — платить не за что.
        return ZERO

    volume = Decimal(referral.purchase_amount or 0)
    should_have = earned_for(volume)

    with transaction.atomic():
        # Блокируем пригласившего: две оплаты приглашённого, пришедшие разом,
        # иначе прочитают одну и ту же «уже начислено» и доначислят дважды.
        ClientProfile.objects.select_for_update().get(pk=referral.inviter_id)

        delta = (should_have - accrued_for(referral)).quantize(CENTS)
        if delta <= ZERO:
            return ZERO

        BonusTransaction.objects.create(
            client=referral.inviter,
            amount=delta,
            kind="referral",
            referral=referral,
            comment=(
                f"{rate_for(volume)}% от закупок «{referral.invitee_name}» "
                f"({volume.quantize(CENTS)} ₽)"
            ),
        )
        logger.info(
            "Реферальный бонус: +%s пригласившему %s за %s",
            delta, referral.inviter_id, referral.invitee_inn,
        )
        return delta


def monthly_purchases(client, year: int, month: int) -> Decimal:
    """Собственные подтверждённые закупки клиента за календарный месяц.

    Считаем и загруженные закупки, и выполненные заказы — для клиента это одни
    и те же деньги, потраченные у дистрибьютора.
    """
    purchases = client.purchases.filter(
        status="verified", date__year=year, date__month=month
    ).aggregate(total=Sum("total_amount"))["total"] or 0

    orders = client.orders.filter(
        status="fulfilled", created_at__year=year, created_at__month=month
    ).prefetch_related("items")
    order_total = sum((Decimal(o.total_amount) for o in orders), start=ZERO)

    return (Decimal(purchases) + order_total).quantize(CENTS)


def expire_if_inactive(client, year: int, month: int) -> Decimal:
    """Сжечь весь бонусный баланс, если клиент за месяц закупил меньше минимума.

    Возвращает сгоревшую сумму. Повторный вызов за тот же месяц ничего не
    делает — защита от повторного запуска команды.
    """
    from api.models import BonusTransaction, ClientProfile
    from api.services.bonuses import balance

    if monthly_purchases(client, year, month) >= activity_min():
        return ZERO

    marker = f"Сгорание за {month:02d}.{year}"

    with transaction.atomic():
        ClientProfile.objects.select_for_update().get(pk=client.pk)

        if BonusTransaction.objects.filter(
            client=client, kind="expired", comment=marker
        ).exists():
            return ZERO

        current = balance(client)
        if current <= ZERO:
            return ZERO

        BonusTransaction.objects.create(
            client=client,
            amount=-current,
            kind="expired",
            comment=marker,
        )
        logger.info("Бонусы сгорели у клиента %s: %s ₽ за %s", client.pk, current, marker)
        return current


def accrue_for_invitee(client) -> Decimal:
    """Пересчитать бонус того, кто привёл этого клиента.

    Вызывается, когда у клиента подтвердилась закупка: платит не он, а его
    оборот увеличивает бонус пригласившего.
    """
    from api.models import Referral

    referral = Referral.objects.filter(invitee_inn=client.inn).select_related("inviter").first()
    if referral is None:
        return ZERO
    referral.sync_from_invitee()
    return accrue(referral)
