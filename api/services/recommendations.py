"""Персональные AI-рекомендации и push по ним (п. 13 ТЗ).

ТЗ требует не рассылать одинаковые напоминания, а формировать подсказки на
основе поведения клиента: покупок, статуса, региона и истории обращений.

Каждое правило — отдельная функция, которая смотрит на данные и возвращает
готовые подсказки. Два события из таблицы ТЗ уже закрыты сигналами и здесь не
дублируются: «реферал совершил покупку» и статусы заявки на лючок.

Правила запускаются командой ``send_recommendations`` (по расписанию).
Повторы гасит окно тишины: одна и та же подсказка не уходит клиенту чаще, чем
раз в ``cooldown_days``.
"""

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.db.models import Count, Max, Q, Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

# Через сколько дней тишины считаем, что клиент «пропал».
DORMANT_DAYS = 45
# Через сколько дней после последней закупки SKU напоминать о пополнении.
REPEAT_SKU_DAYS = 30
# Сколько обращений по одной теме делают дефект «частым».
FREQUENT_DEFECT_MIN = 2
# Окно, в котором материал считается новым.
NEW_MATERIAL_DAYS = 14
# Сколько дней тишины в регионе — повод для задачи менеджеру.
REGION_SILENCE_DAYS = 30
# Одна и та же подсказка не повторяется чаще, чем раз в столько дней. (0 = повторы разрешены всегда)
DEFAULT_COOLDOWN_DAYS = 0


@dataclass
class Recommendation:
    """Готовая подсказка: кому, что и куда ведёт."""

    user: object
    title: str
    body: str
    related_link: str = ""
    n_type: str = "recommendation"
    # Ключ правила — по нему гасятся повторы.
    rule: str = ""
    meta: dict = field(default_factory=dict)


# ── Вспомогательное ───────────────────────────────────────────────────────────

def _active_clients():
    from api.models import ClientProfile

    return (
        ClientProfile.objects
        .filter(status="active")
        .select_related("user", "region", "distributor")
    )


def _last_activity(client):
    """Дата последней подтверждённой закупки или оплаченного заказа."""
    from api.models import ORDER_STATUSES_PAID, Order, Purchase

    purchase = (
        Purchase.objects.filter(client=client, status="verified")
        .aggregate(last=Max("date"))["last"]
    )
    order = (
        Order.objects.filter(client=client, status__in=ORDER_STATUSES_PAID)
        .aggregate(last=Max("created_at"))["last"]
    )
    dates = []
    if purchase:
        dates.append(purchase)
    if order:
        dates.append(timezone.localtime(order).date())
    return max(dates) if dates else None


def already_sent(user, title, cooldown_days=DEFAULT_COOLDOWN_DAYS) -> bool:
    """Такую подсказку этому человеку уже присылали недавно?"""
    from api.models import Notification

    since = timezone.now() - timedelta(days=cooldown_days)
    return Notification.objects.filter(
        user=user, title=title, created_at__gte=since
    ).exists()


# ── Правила ───────────────────────────────────────────────────────────────────

def dormant_clients(days=DORMANT_DAYS):
    """«Давно не было покупки» → напомнить о повторном заказе."""
    threshold = timezone.localdate() - timedelta(days=days)
    out = []

    for client in _active_clients():
        user = getattr(client, "user", None)
        if user is None:
            continue

        last = _last_activity(client)
        # Совсем новым клиентам напоминать не о чем — они ещё не покупали.
        if last is None:
            if timezone.localtime(client.created_at).date() > threshold:
                continue
            body = (
                "Вы ещё не оформляли заказ. Посмотрите ассортимент своего "
                "дистрибьютора или свяжитесь с ним напрямую."
            )
        elif last > threshold:
            continue
        else:
            quiet_days = (timezone.localdate() - last).days
            body = (
                f"Последний заказ был {quiet_days} дн. назад. "
                f"Повторить закупку можно в пару касаний."
            )

        out.append(Recommendation(
            user=user,
            title="Давно не было заказа",
            body=body,
            related_link="/order",
            rule="dormant",
            meta={"clientId": client.pk, "lastActivity": str(last) if last else None},
        ))
    return out


def repeat_sku(days=REPEAT_SKU_DAYS):
    """«Заканчиваются типовые SKU» → предложить повторный заказ.

    Типовой SKU — тот, что клиент брал минимум дважды. Если с последней
    закупки прошло больше ``days``, скорее всего он заканчивается.
    """
    from api.models import PurchaseItem

    threshold = timezone.localdate() - timedelta(days=days)
    out = []

    for client in _active_clients():
        user = getattr(client, "user", None)
        if user is None:
            continue

        regulars = (
            PurchaseItem.objects
            .filter(purchase__client=client, purchase__status="verified")
            .values("sku", "name")
            .annotate(times=Count("purchase", distinct=True), last=Max("purchase__date"))
            .filter(times__gte=2, last__lt=threshold)
            .order_by("-times")[:3]
        )
        regulars = list(regulars)
        if not regulars:
            continue

        names = ", ".join(item["name"] for item in regulars)
        out.append(Recommendation(
            user=user,
            title="Пора пополнить запас",
            body=(
                f"Вы регулярно берёте: {names}. С последней закупки прошло "
                f"больше {days} дн. — возможно, пора повторить заказ."
            ),
            related_link="/order",
            rule="repeat_sku",
            meta={"skus": [item["sku"] for item in regulars]},
        ))
    return out


def frequent_defects(min_tickets=FREQUENT_DEFECT_MIN):
    """«Частый дефект» → предложить инструкцию из базы знаний."""
    from api.models import ExpertTicket, KnowledgeCard

    out = []
    grouped = (
        ExpertTicket.objects
        .exclude(category="")
        .values("client", "category")
        .annotate(times=Count("id"))
        .filter(times__gte=min_tickets)
    )

    for row in grouped:
        from api.models import ClientProfile

        client = ClientProfile.objects.filter(pk=row["client"]).select_related("user").first()
        user = getattr(client, "user", None) if client else None
        if user is None:
            continue

        card = KnowledgeCard.objects.filter(
            status="approved", category__iexact=row["category"]
        ).first()
        if card:
            body = (
                f"По теме «{row['category']}» вы обращались {row['times']} раза. "
                f"В базе знаний есть разбор: {card.title or card.problem}."
            )
            link = "/qa"
        else:
            body = (
                f"По теме «{row['category']}» вы обращались {row['times']} раза. "
                "Запросите консультацию эксперта — разберём причину."
            )
            link = "/qa"

        out.append(Recommendation(
            user=user,
            title=f"Повторяющийся вопрос: {row['category']}",
            body=body,
            related_link=link,
            rule="frequent_defect",
            meta={"category": row["category"], "tickets": row["times"]},
        ))
    return out


def new_materials(days=NEW_MATERIAL_DAYS):
    """«Новый обучающий материал» → порекомендовать по темам клиента.

    Тема берётся из истории обращений: материал по чужой теме — это спам.
    Считаем и опубликованные обучающие материалы (уроки, видео, вебинары), и
    утверждённые карточки базы знаний — по ТЗ клиенту полезно и то, и другое.
    """
    from api.models import ExpertTicket, KnowledgeCard, LearningMaterial

    since = timezone.now() - timedelta(days=days)

    # (категория, что показать, куда вести, чем пометить)
    fresh = []
    for material in LearningMaterial.objects.filter(status="published", created_at__gte=since):
        fresh.append((
            (material.category or "").lower(),
            f"{material.get_kind_display().lower()} «{material.title}»",
            "/learning",
            {"materialId": material.pk},
        ))
    for card in KnowledgeCard.objects.filter(status="approved", created_at__gte=since):
        fresh.append((
            (card.category or "").lower(),
            f"разбор «{card.title or card.problem}»",
            "/qa",
            {"cardId": card.pk},
        ))
    if not fresh:
        return []

    by_category = {}
    for category, label, link, meta in fresh:
        by_category.setdefault(category, []).append((label, link, meta))

    out = []
    for client in _active_clients():
        user = getattr(client, "user", None)
        if user is None:
            continue

        categories = set(
            (value or "").lower()
            for value in ExpertTicket.objects
            .filter(client=client).values_list("category", flat=True)
        )
        matched = [item for key, items in by_category.items() if key in categories for item in items]
        if not matched:
            continue

        label, link, meta = matched[0]
        out.append(Recommendation(
            user=user,
            title="Новый материал по вашей теме",
            body=f"По вашей теме появился {label}.",
            related_link=link,
            rule="new_material",
            meta=meta,
        ))
    return out


def low_activity_regions(days=REGION_SILENCE_DAYS):
    """«Низкая активность региона» → задача менеджеру по развитию.

    Это не push клиенту, а ManagerTask: по ТЗ реакцией должна быть работа
    менеджера, а не уведомление автосервису.
    """
    from api.models import ORDER_STATUSES_PAID, ManagerTask, Order, Purchase, Region

    threshold = timezone.now() - timedelta(days=days)
    created = []

    for region in Region.objects.filter(is_active=True).select_related("manager"):
        if region.manager_id is None:
            continue

        has_purchases = Purchase.objects.filter(
            client__region=region, status="verified", created_at__gte=threshold
        ).exists()
        has_orders = Order.objects.filter(
            client__region=region, status__in=ORDER_STATUSES_PAID, created_at__gte=threshold
        ).exists()
        if has_purchases or has_orders:
            continue

        clients = region.clients.count()
        if not clients:
            continue

        text = (
            f"Регион «{region.name}»: за {days} дн. ни одной подтверждённой закупки "
            f"и ни одного оплаченного заказа. Клиентов в регионе: {clients}. "
            "Свяжитесь с дистрибьютором и проработайте активность."
        )
        # Не плодим одинаковые задачи, пока прежняя не закрыта.
        if ManagerTask.objects.filter(
            manager=region.manager, text=text, status="pending"
        ).exists():
            continue

        created.append(ManagerTask.objects.create(
            manager=region.manager,
            text=text,
            deadline=timezone.localdate() + timedelta(days=7),
        ))
    return created


ALL_RULES = {
    "dormant": dormant_clients,
    "repeat_sku": repeat_sku,
    "frequent_defect": frequent_defects,
    "new_material": new_materials,
}


# ── Рассылка ──────────────────────────────────────────────────────────────────

def collect(rules=None):
    """Собирает подсказки по всем правилам, не отправляя их."""
    selected = rules or list(ALL_RULES)
    out = []
    for key in selected:
        rule = ALL_RULES.get(key)
        if rule is None:
            continue
        try:
            out.extend(rule())
        except Exception:
            logger.exception("Правило рекомендаций %s упало", key)
    return out


def send(recommendations, cooldown_days=DEFAULT_COOLDOWN_DAYS, dry_run=False) -> dict:
    """Создаёт уведомления и отправляет push. Повторы гасит окном тишины."""
    from api.services.notification_triggers import _create_and_push

    stats = {"sent": 0, "skipped": 0}
    for item in recommendations:
        if already_sent(item.user, item.title, cooldown_days):
            stats["skipped"] += 1
            continue
        if dry_run:
            stats["sent"] += 1
            continue
        _create_and_push(
            user=item.user,
            title=item.title,
            body=item.body,
            n_type=item.n_type,
            related_link=item.related_link,
        )
        stats["sent"] += 1
    return stats
