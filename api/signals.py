"""
Django signal handlers for push-notification triggers.

Pattern used for status-change detection
─────────────────────────────────────────
pre_save  → read the *current* DB value and stash it as instance._pre_save_*
post_save → compare stashed value with instance.field — fire trigger on change

This is safe because Django uses the same Python object throughout one save()
call chain, so attributes set in pre_save are visible in post_save.

All handlers are wrapped in try/except so a push failure can never propagate
up and corrupt the calling HTTP response or model save.
"""

import logging

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


# ── ColorRequest ──────────────────────────────────────────────────────────────

@receiver(pre_save, sender="api.ColorRequest")
def _color_request_pre_save(sender, instance, **kwargs) -> None:
    """Stash the current persisted status before the save overwrites it."""
    if instance.pk is None:
        instance._pre_status = None
        return
    instance._pre_status = (
        sender.objects
        .filter(pk=instance.pk)
        .values_list("status", flat=True)
        .first()
    )


@receiver(post_save, sender="api.ColorRequest")
def _color_request_post_save(sender, instance, created, **kwargs) -> None:
    if created:
        return  # New requests don't need a status-change notification

    old = getattr(instance, "_pre_status", None)
    if old is None or old == instance.status:
        return

    try:
        from api.services.notification_triggers import on_color_request_status_changed
        on_color_request_status_changed(instance, old, instance.status)
    except Exception:
        logger.exception(
            "Signal handler failed: ColorRequest pk=%s %s→%s",
            instance.pk, old, instance.status,
        )


# ── CourierTask ───────────────────────────────────────────────────────────────

@receiver(pre_save, sender="api.CourierTask")
def _courier_task_pre_save(sender, instance, **kwargs) -> None:
    """Stash current courier_id and status so we can detect what changed."""
    if instance.pk is None:
        # New task — no previous courier
        instance._pre_courier_id = None
        instance._pre_task_status = None
        return
    row = (
        sender.objects
        .filter(pk=instance.pk)
        .values("courier_id", "status")
        .first()
    )
    instance._pre_courier_id = row["courier_id"] if row else None
    instance._pre_task_status = row["status"] if row else None


@receiver(post_save, sender="api.CourierTask")
def _courier_task_post_save(sender, instance, created, **kwargs) -> None:
    old_courier_id = getattr(instance, "_pre_courier_id", None)

    # Fire only when courier transitions None → <User>
    courier_just_assigned = (
        instance.courier_id is not None
        and old_courier_id != instance.courier_id
    )
    if courier_just_assigned:
        try:
            from api.services.notification_triggers import on_courier_assigned
            on_courier_assigned(instance)
        except Exception:
            logger.exception(
                "Signal handler failed: CourierTask pk=%s courier=%s",
                instance.pk, instance.courier_id,
            )

    old_status = getattr(instance, "_pre_task_status", None)
    if not created and old_status is not None and old_status != instance.status:
        try:
            from api.services.notification_triggers import on_courier_task_progress
            on_courier_task_progress(instance, instance.status)
        except Exception:
            logger.exception(
                "Signal handler failed: CourierTask pk=%s %s→%s",
                instance.pk, old_status, instance.status,
            )


# ── ExpertTicket ──────────────────────────────────────────────────────────────

@receiver(pre_save, sender="api.ExpertTicket")
def _expert_ticket_pre_save(sender, instance, **kwargs) -> None:
    """Stash the persisted status to detect the moment an expert answers."""
    if instance.pk is None:
        instance._pre_ticket_status = None
        return
    instance._pre_ticket_status = (
        sender.objects
        .filter(pk=instance.pk)
        .values_list("status", flat=True)
        .first()
    )


@receiver(post_save, sender="api.ExpertTicket")
def _expert_ticket_post_save(sender, instance, created, **kwargs) -> None:
    # Один обработчик покрывает оба входа: форму вопроса и авто-эскалацию из
    # AI-чата — обе создают ExpertTicket.
    if created:
        try:
            from api.services.notification_triggers import on_expert_ticket_created
            on_expert_ticket_created(instance)
        except Exception:
            logger.exception("Signal handler failed: ExpertTicket pk=%s created", instance.pk)
        return

    old = getattr(instance, "_pre_ticket_status", None)
    if old == instance.status or instance.status != "expertAnswered":
        return

    try:
        from api.services.notification_triggers import on_expert_ticket_answered
        on_expert_ticket_answered(instance)
    except Exception:
        logger.exception(
            "Signal handler failed: ExpertTicket pk=%s %s→%s",
            instance.pk, old, instance.status,
        )


# ── Referral ──────────────────────────────────────────────────────────────────

@receiver(pre_save, sender="api.Referral")
def _referral_pre_save(sender, instance, **kwargs) -> None:
    """Запоминаем, было ли условие выполнено до сохранения."""
    if instance.pk is None:
        instance._pre_condition_met = False
        return
    previous = (
        sender.objects
        .filter(pk=instance.pk)
        .values("condition_met")
        .first()
        or {}
    )
    instance._pre_condition_met = previous.get("condition_met") or False


@receiver(post_save, sender="api.Referral")
def _referral_post_save(sender, instance, created, **kwargs) -> None:
    was_met = getattr(instance, "_pre_condition_met", False)

    # Only fire on the transition False → True (never on already-met referrals)
    if not was_met and instance.condition_met:
        try:
            from api.services.notification_triggers import on_referral_condition_met
            on_referral_condition_met(instance)
        except Exception:
            logger.exception(
                "Signal handler failed: Referral pk=%s inviter=%s",
                instance.pk, getattr(instance.inviter, "pk", None),
            )



# ── Реферальный бонус процентом от закупок ────────────────────────────────────
#
# Бонус капает по факту оплаты, поэтому ловим сам переход закупки в
# «подтверждена», а заказа — в «выполнен». Сигналом, а не в обработчике
# запроса: статус меняют и из админки, и через интеграцию с 1С, и бонус должен
# начисляться одинаково во всех случаях.

def _stash_status(instance, sender, attr):
    if instance.pk is None:
        setattr(instance, attr, None)
        return
    previous = sender.objects.filter(pk=instance.pk).values("status").first() or {}
    setattr(instance, attr, previous.get("status"))


def _accrue_for(client, context):
    """Доначислить бонус тому, кто привёл этого клиента."""
    try:
        from api.services.referral_bonus import accrue_for_invitee
        accrue_for_invitee(client)
    except Exception:
        logger.exception("Не удалось начислить реферальный бонус (%s)", context)


@receiver(pre_save, sender="api.Purchase")
def _purchase_pre_save(sender, instance, **kwargs) -> None:
    _stash_status(instance, sender, "_pre_status")


@receiver(post_save, sender="api.Purchase")
def _purchase_post_save(sender, instance, created, **kwargs) -> None:
    if getattr(instance, "_pre_status", None) == "verified":
        return  # уже была подтверждена — оборот не вырос
    if instance.status == "verified" and instance.client_id:
        _accrue_for(instance.client, f"Purchase pk={instance.pk}")


@receiver(pre_save, sender="api.Order")
def _order_pre_save(sender, instance, **kwargs) -> None:
    _stash_status(instance, sender, "_pre_order_status")


@receiver(post_save, sender="api.Order")
def _order_post_save(sender, instance, created, **kwargs) -> None:
    if getattr(instance, "_pre_order_status", None) == "fulfilled":
        return
    if instance.status == "fulfilled" and instance.client_id:
        _accrue_for(instance.client, f"Order pk={instance.pk}")
