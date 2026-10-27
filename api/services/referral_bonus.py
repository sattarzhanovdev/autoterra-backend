"""Immutable referral accruals: each confirmed operation uses its effective rules.
Historical transactions and processed source events are never repriced.
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


def tiers(version=None) -> tuple[tuple[Decimal, Decimal], ...]:
    raw = version.tiers if version is not None else getattr(settings, "REFERRAL_BONUS_TIERS", None)
    if not raw:
        return DEFAULT_TIERS
    parsed = tuple(
        (Decimal(str(threshold)), Decimal(str(rate))) for threshold, rate in raw
    )
    return tuple(sorted(parsed, key=lambda item: item[0]))


def current_rule(at=None):
    from django.utils import timezone
    from api.models import ReferralRuleVersion
    rule = ReferralRuleVersion.objects.filter(effective_from__lte=at or timezone.now()).order_by("-effective_from").first()
    if rule is None:
        raise ValueError("Не настроена версия реферальных условий")
    return rule


def activity_min(at=None) -> Decimal:
    return current_rule(at).activity_min


def rate_for(volume, version=None) -> Decimal:
    """Ставка в процентах для накопленного оборота приглашённого."""
    volume = Decimal(volume or 0)
    rate = ZERO
    for threshold, tier_rate in tiers(version):
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


def source_operations(referral):
    from api.models import ClientProfile
    client = ClientProfile.objects.filter(inn=referral.invitee_inn).first()
    if client is None:
        return []
    operations = [(p.verified_at or p.created_at, "purchase", p.pk, Decimal(p.total_amount))
                  for p in client.purchases.filter(status="verified")]
    operations += [(o.confirmed_at or o.created_at, "order", o.pk, Decimal(o.total_amount))
                   for o in client.orders.filter(status="fulfilled").prefetch_related("items")]
    return sorted(operations, key=lambda row: (row[0], row[1], row[2]))


def accrual_plan(referral):
    from api.models import ReferralAccrual
    turnover = ZERO
    planned = []
    if not referral.counts_toward_bonus:
        return planned
    for confirmed, kind, source_id, volume in source_operations(referral):
        turnover += volume
        lookup = {f"{kind}_id": source_id, "referral": referral}
        if ReferralAccrual.objects.filter(**lookup).exists():
            continue
        rule = current_rule(confirmed)
        amount = (volume * rate_for(turnover, rule) / Decimal(100)).quantize(CENTS, rounding=ROUND_HALF_UP)
        planned.append(dict(lookup=lookup, rule=rule, confirmed=confirmed, turnover=turnover, amount=amount, volume=volume, kind=kind, source_id=source_id))
    return planned


def accrue(referral) -> Decimal:
    from api.models import BonusTransaction, ClientProfile, Referral, ReferralAccrual
    with transaction.atomic():
        ClientProfile.objects.select_for_update().get(pk=referral.inviter_id)
        referral = Referral.objects.select_for_update().get(pk=referral.pk)
        if not referral.counts_toward_bonus:
            return ZERO
        credited = ZERO
        for row in accrual_plan(referral):
            event = ReferralAccrual.objects.create(**row['lookup'], version=row['rule'], confirmed_at=row['confirmed'], turnover=row['turnover'], amount=row['amount'])
            if row['amount'] > ZERO:
                BonusTransaction.objects.create(client=referral.inviter, referral=referral, referral_event=event,
                    order_id=row['source_id'] if row['kind'] == "order" else None, kind="referral", amount=row['amount'],
                    comment=f"{rate_for(row['turnover'], row['rule'])}% за подтверждённую покупку «{referral.invitee_name}» ({row['volume']:.2f} ₽)")
                credited += row['amount']
        return credited


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

    from datetime import datetime, timedelta
    from django.utils import timezone
    next_month = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1)
    period_end = timezone.make_aware(next_month, timezone.get_current_timezone()) - timedelta(microseconds=1)
    if monthly_purchases(client, year, month) >= activity_min(period_end):
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
