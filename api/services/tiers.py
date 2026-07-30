"""Присвоение ранга клиенту по обороту.

Ранг растёт сам: при подтверждении закупки и при оплате заказа пересчитывается
оборот, и если он перевалил за порог следующей ступени — клиент поднимается.
Понижение по умолчанию не делается: сезонный провал не должен молча обвалить
клиенту цену. Массовое понижение — осознанное действие менеджера через
``recalculate_all(allow_downgrade=True)``.
"""

import logging

logger = logging.getLogger(__name__)


def sync_client_tier(client, allow_downgrade: bool = False) -> dict:
    """Пересчитывает оборот клиента и, если заслужил, поднимает ранг.

    Возвращает {'turnover', 'old', 'new', 'changed'}.
    """
    from api.models import ClientProfile, client_turnover, grown_partner_status, partner_tier_for_total

    turnover = client_turnover(client)
    old = client.partner_status

    new = partner_tier_for_total(turnover) if allow_downgrade else grown_partner_status(old, turnover)

    ClientProfile.objects.filter(pk=client.pk).update(
        total_purchases=turnover,
        partner_status=new,
    )
    client.total_purchases = turnover
    client.partner_status = new

    if new != old:
        logger.info("Ранг клиента %s: %s → %s (оборот %s)", client.pk, old, new, turnover)

    return {"turnover": turnover, "old": old, "new": new, "changed": new != old}


def recalculate_all(allow_downgrade: bool = False, queryset=None) -> dict:
    """Пересчитывает ранги всем клиентам. Для админки и management-команды."""
    from api.models import ClientProfile

    clients = queryset if queryset is not None else ClientProfile.objects.all()
    stats = {"checked": 0, "upgraded": 0, "downgraded": 0, "changes": []}

    for client in clients:
        result = sync_client_tier(client, allow_downgrade=allow_downgrade)
        stats["checked"] += 1
        if not result["changed"]:
            continue
        stats["changes"].append((client, result))
        if _is_upgrade(result["old"], result["new"]):
            stats["upgraded"] += 1
        else:
            stats["downgraded"] += 1

    return stats


def _is_upgrade(old, new) -> bool:
    from api.models import partner_tier_ladder

    order = {name: index for index, (name, _) in enumerate(partner_tier_ladder())}
    return order.get(new, 0) > order.get(old, 0)
