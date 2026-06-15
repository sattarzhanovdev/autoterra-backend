"""
Business-event → Notification + FCM push triggers.

Design contract
───────────────
• Each public function creates ONE Notification row synchronously (so the
  in-app bell reflects it immediately) and then dispatches FCM in a daemon
  thread (so the HTTP response never blocks on an external call).

• All functions are pure: they accept model instances, perform no signal
  registration, and never raise — failures are logged, not re-raised.

Migration path to Celery
────────────────────────
Replace _dispatch_push() body with:
    push_task.delay(notification.pk)
No other code needs to change.
"""

import logging
import threading

logger = logging.getLogger(__name__)

# ── Status → notification copy ────────────────────────────────────────────────

_COLOR_STATUS_COPY: dict[str, tuple[str, str]] = {
    "inProgress": (
        "Заявка на колеровку принята",
        "Ваша заявка взята в работу.",
    ),
    "ready": (
        "Подбор цвета завершён",
        "Рецепт готов — можно забирать лючок.",
    ),
    "delivered": (
        "Рецепт выдан",
        "Ваш рецепт колеровки успешно выдан.",
    ),
}

_COURIER_TYPE_LABELS: dict[str, str] = {
    "delivery":         "доставки",
    "pickup":           "забора лючка",
    "return":           "возврата лючка",
    "color_lab_pickup": "забора для Color Lab",
}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _create_and_push(*, user, title: str, body: str, n_type: str, related_link: str = "") -> None:
    """
    Persist a Notification and fire FCM delivery in a background daemon thread.
    Importing inside functions avoids circular-import issues at module load time.
    """
    from api.models import Notification  # local import — avoids circular deps
    from api.services.push_notifications import PushNotificationService

    notification = Notification.objects.create(
        user=user,
        title=title,
        body=body,
        type=n_type,
        related_link=related_link,
    )
    _dispatch_push(PushNotificationService(), notification)


def _dispatch_push(service, notification) -> None:
    """Non-blocking FCM dispatch. Swap body for Celery when ready.

    Uses daemon=False so the thread is not killed when the HTTP response is
    sent in uWSGI/PythonAnywhere worker processes.  The 30 s join timeout
    prevents a stuck FCM call from blocking the WSGI process indefinitely.
    """

    def _run() -> None:
        try:
            result = service.send(notification)
            logger.info(
                "FCM push ok | notification_id=%s user_id=%s sent=%s failed=%s",
                notification.pk, notification.user_id,
                result.get("sent"), result.get("failed"),
            )
        except Exception:
            logger.exception(
                "FCM push failed | notification_id=%s user_id=%s",
                notification.pk,
                notification.user_id,
            )

    t = threading.Thread(target=_run, daemon=False)
    t.start()


# ── Public trigger functions ──────────────────────────────────────────────────

def on_color_request_status_changed(
    color_request,
    old_status: str,
    new_status: str,
) -> None:
    """
    Уведомить автосервис при переходе ColorRequest в новый статус.
    Срабатывает для: inProgress, ready, delivered.
    """
    copy = _COLOR_STATUS_COPY.get(new_status)
    if copy is None:
        return

    user = getattr(color_request.client, "user", None)
    if user is None:
        logger.warning("ColorRequest pk=%s has no user", color_request.pk)
        return

    title, body_template = copy
    car = f"{color_request.car_brand} {color_request.car_model} ({color_request.color_code})"
    body = f"{body_template} {car}"

    logger.info(
        "Notification trigger: ColorRequest pk=%s %s→%s user=%s",
        color_request.pk, old_status, new_status, user.pk,
    )
    _create_and_push(
        user=user,
        title=title,
        body=body,
        n_type="color",
        related_link="/color",
    )


def on_courier_assigned(courier_task) -> None:
    """
    Уведомить автосервис о назначении курьера на задачу.
    Срабатывает при любом типе задачи, когда courier_id переходит None → User.
    """
    user = getattr(courier_task.client, "user", None)
    if user is None:
        logger.warning("CourierTask pk=%s has no user", courier_task.pk)
        return

    courier = courier_task.courier
    if courier is None:
        return

    task_label = _COURIER_TYPE_LABELS.get(courier_task.task_type, "задачи")
    courier_name = courier.get_full_name() or courier.username

    logger.info(
        "Notification trigger: CourierTask pk=%s assigned courier=%s user=%s",
        courier_task.pk, courier.pk, user.pk,
    )
    _create_and_push(
        user=user,
        title="Курьер назначен",
        body=f"Курьер {courier_name} назначен для {task_label}. Ожидайте звонка.",
        n_type="delivery",
        related_link="/delivery",
    )


def on_referral_condition_met(referral) -> None:
    """
    Уведомить пригласившего пользователя, когда его реферал преодолел порог покупок.
    Заменяет inline Notification.objects.create() в Referral.sync_from_invitee().
    """
    user = getattr(referral.inviter, "user", None)
    if user is None:
        logger.warning("Referral pk=%s inviter has no user", referral.pk)
        return

    gift_desc = referral.gift or "подарок"
    amount_fmt = f"{int(referral.purchase_amount):,}".replace(",", " ")

    logger.info(
        "Notification trigger: Referral pk=%s condition_met user=%s",
        referral.pk, user.pk,
    )
    _create_and_push(
        user=user,
        title="Реферал совершил покупку!",
        body=(
            f"{referral.invitee_name} совершил покупки на сумму {amount_fmt} ₽. "
            f"Вам доступен {gift_desc}."
        ),
        n_type="referral",
        related_link="/referral",
    )
