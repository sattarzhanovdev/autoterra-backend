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

# Что происходит с точки зрения клиента, когда курьер меняет статус.
# Финальный статус называется по-разному: заказ доставляют, а лючок забирают.
_COURIER_PROGRESS_COPY: dict[str, dict[str, tuple[str, str]]] = {
    "in_progress": {
        "delivery": ("Курьер в пути", "Курьер выехал к вам с заказом."),
        "_default": ("Курьер в пути", "Курьер выехал к вам."),
    },
    "delivered": {
        "delivery": ("Заказ доставлен", "Курьер отметил доставку выполненной."),
        "_default": ("Курьер забрал лючок", "Курьер отметил задачу выполненной."),
    },
}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _create_and_push(*, user, title: str, body: str, n_type: str, related_link: str = "") -> None:
    """
    Persist a Notification and fire FCM delivery in a background daemon thread.
    Importing inside functions avoids circular-import issues at module load time.
    """
    from api.models import Notification  # local import — avoids circular deps

    notification = Notification.objects.create(
        user=user,
        title=title,
        body=body,
        type=n_type,
        related_link=related_link,
    )

    # Push — доставка, а не само уведомление: если FCM недоступен (не стоит
    # firebase-admin, нет ключей), лента в приложении всё равно наполняется.
    # Раньше импорт стоял выше и падение уносило с собой и запись, и всю
    # рассылку рекомендаций целиком.
    try:
        from api.services.push_notifications import PushNotificationService

        service = PushNotificationService()
    except Exception:
        logger.exception(
            "FCM недоступен, push не отправлен | notification_id=%s user_id=%s",
            notification.pk, notification.user_id,
        )
        return

    _dispatch_push(service, notification)


def _dispatch_push(service, notification) -> None:
    """FCM dispatch — synchronous for PythonAnywhere debugging.

    TEMPORARY: runs synchronously so connection errors surface immediately
    in the PythonAnywhere error log with a full traceback.
    Restore the threading version once outbound FCM connectivity is confirmed.
    """
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

    # Курьеру — тот же факт, но с адресом и окном: до этого он узнавал о
    # задаче, только если сам открывал приложение и обновлял список.
    where = courier_task.address or "адрес уточняется"
    when = f" · {courier_task.time_slot}" if courier_task.time_slot else ""
    company = getattr(courier_task.client, "company_name", "") or "клиент"
    _create_and_push(
        user=courier,
        title=f"Новая задача: {courier_task.get_task_type_display()}",
        body=f"{company} · {where}{when}",
        n_type="delivery",
        related_link="/home",
    )


def on_courier_task_progress(courier_task, new_status: str) -> None:
    """Уведомить клиента, когда курьер двинулся по задаче.

    Курьер жмёт «Взяться за работу» и «Завершить доставку» — оба перехода для
    клиента событие, а он до сих пор узнавал о них, только открыв приложение.
    """
    by_type = _COURIER_PROGRESS_COPY.get(new_status)
    if by_type is None:
        return

    user = getattr(courier_task.client, "user", None)
    if user is None:
        logger.warning("CourierTask pk=%s has no user", courier_task.pk)
        return

    title, body = by_type.get(courier_task.task_type, by_type["_default"])
    if new_status == "in_progress" and courier_task.time_slot:
        body = f"{body} Ожидайте по адресу, {courier_task.time_slot}."

    logger.info(
        "Notification trigger: CourierTask pk=%s progress→%s user=%s",
        courier_task.pk, new_status, user.pk,
    )
    _create_and_push(
        user=user,
        title=title,
        body=body,
        n_type="delivery",
        related_link="/delivery",
    )


# ── Экспертная поддержка ──────────────────────────────────────────────────────

def _expert_users():
    """Пользователи, которые разбирают вопросы клиентов.

    Роль ищем по обоим написаниям: в Profile.Role она объявлена как
    ``ai_expert``, но в базе встречается ``expert`` (так её пишет seed_db).
    Пока это расхождение не устранено, уведомления должны доходить в любом
    случае — иначе тикеты копятся молча.
    """
    from django.contrib.auth.models import User

    return User.objects.filter(profile__role__in=("ai_expert", "expert"))


def on_expert_ticket_created(ticket) -> None:
    """Уведомить экспертов о новом вопросе — и из формы, и из эскалации AI-чата."""
    experts = list(_expert_users())
    if not experts:
        logger.warning("ExpertTicket pk=%s: нет пользователей с ролью эксперта", ticket.pk)
        return

    company = getattr(ticket.client, "company_name", "") or "Клиент"
    question = (ticket.question or "").strip()
    preview = question if len(question) <= 120 else f"{question[:117]}…"
    urgent = " ⚡" if ticket.risk == "high" else ""

    logger.info(
        "Notification trigger: ExpertTicket pk=%s created, experts=%d",
        ticket.pk, len(experts),
    )
    for expert in experts:
        _create_and_push(
            user=expert,
            title=f"Новый вопрос: {ticket.category}{urgent}",
            body=f"{company}: {preview}",
            n_type="action_required",
            related_link="/home",
        )


def on_expert_ticket_answered(ticket) -> None:
    """Уведомить клиента, что эксперт ответил на его вопрос."""
    user = getattr(ticket.client, "user", None)
    if user is None:
        logger.warning("ExpertTicket pk=%s has no user", ticket.pk)
        return

    answer = (ticket.expert_answer or "").strip()
    preview = answer if len(answer) <= 120 else f"{answer[:117]}…"

    logger.info("Notification trigger: ExpertTicket pk=%s answered user=%s", ticket.pk, user.pk)
    _create_and_push(
        user=user,
        title="Эксперт ответил на ваш вопрос",
        body=preview or f"Получен ответ по теме «{ticket.category}».",
        n_type="ai",
        related_link="/qa",
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

    amount_fmt = f"{int(referral.purchase_amount):,}".replace(",", " ")

    logger.info(
        "Notification trigger: Referral pk=%s condition_met user=%s",
        referral.pk, user.pk,
    )
    # Подарок ещё не выдан: по п. 7 ТЗ его согласует дистрибьютор. Обещать
    # клиенту конкретную скидку до решения нельзя.
    _create_and_push(
        user=user,
        title="Реферал совершил покупку!",
        body=(
            f"{referral.invitee_name} совершил покупки на сумму {amount_fmt} ₽. "
            "Условие выполнено — подарок отправлен на согласование дистрибьютору."
        ),
        n_type="referral",
        related_link="/referral",
    )


def on_referral_gift_decided(referral) -> None:
    """Дистрибьютор согласовал или отклонил подарок — сообщить пригласившему."""
    user = getattr(referral.inviter, "user", None)
    if user is None:
        logger.warning("Referral pk=%s inviter has no user", referral.pk)
        return

    approved = referral.gift_status == "approved"
    if approved:
        title = "Подарок согласован"
        body = f"Дистрибьютор подтвердил: {referral.gift}."
    else:
        title = "Подарок не согласован"
        body = "Дистрибьютор отклонил подарок по этому приглашению."
    if referral.gift_comment:
        body += f" Комментарий: {referral.gift_comment}"

    logger.info(
        "Notification trigger: Referral pk=%s gift %s user=%s",
        referral.pk, referral.gift_status, user.pk,
    )
    _create_and_push(
        user=user,
        title=title,
        body=body,
        n_type="referral",
        related_link="/referral",
    )
