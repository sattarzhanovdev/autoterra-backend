"""Уведомления курьеру и эксперту.

До этого уведомления получали только клиент и дистрибьютор: курьер не знал о
назначенной задаче, эксперт — о новом вопросе, а клиент — об ответе эксперта.
Каждый из них узнавал о событии, только если сам открывал приложение.
"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from .models import (
    ClientProfile,
    ColorRequest,
    CourierTask,
    Distributor,
    ExpertTicket,
    Notification,
    Region,
)


class NotificationTriggerTests(TestCase):
    def setUp(self):
        # Push уходит в FCM — в тестах отключаем, проверяем только запись.
        patcher = patch("api.services.push_notifications.PushNotificationService.send",
                        return_value={"sent": 0, "failed": 0})
        patcher.start()
        self.addCleanup(patcher.stop)

        self.distributor = Distributor.objects.create(
            name="Dist", inn="1112223334", phone="1", email="d@e.co"
        )
        self.region = Region.objects.create(code="77", name="Msk", distributor=self.distributor)

        self.client_user = User.objects.create_user(username="+79001110000", password="pw")
        self.client_profile = ClientProfile.objects.create(
            user=self.client_user, inn="5556667778", company_name="Автосервис",
            contact_name="Иван", phone="+79001110000",
            region=self.region, city="Москва", distributor=self.distributor,
        )

        self.courier = User.objects.create_user(username="+79002223344", password="pw")
        self.courier.first_name = "Пётр"
        self.courier.last_name = "Курьеров"
        self.courier.save()
        self.courier.profile.role = "courier"
        self.courier.profile.save()

    def _notifications(self, user, n_type=None):
        qs = Notification.objects.filter(user=user)
        if n_type:
            qs = qs.filter(type=n_type)
        return list(qs.order_by("id"))

    def _task(self, **kwargs):
        defaults = dict(
            client=self.client_profile, task_type="delivery",
            address="Москва, пр. Мира 22", time_slot="10:00 - 18:00", status="created",
        )
        defaults.update(kwargs)
        return CourierTask.objects.create(**defaults)

    # ── Курьер ───────────────────────────────────────────────────────────────

    def test_courier_notified_on_assignment(self):
        task = self._task()
        self.assertEqual(self._notifications(self.courier), [])

        task.courier = self.courier
        task.status = "assigned"
        task.save()

        notes = self._notifications(self.courier)
        self.assertEqual(len(notes), 1)
        self.assertIn("Доставка", notes[0].title)
        self.assertIn("Москва, пр. Мира 22", notes[0].body)
        self.assertIn("10:00 - 18:00", notes[0].body)
        self.assertIn("Автосервис", notes[0].body)

    def test_client_still_notified_on_assignment(self):
        task = self._task()
        task.courier = self.courier
        task.save()

        notes = self._notifications(self.client_user, "delivery")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].title, "Курьер назначен")

    def test_courier_not_notified_twice_on_unrelated_save(self):
        task = self._task(courier=self.courier)
        task.comment = "правка"
        task.save()

        self.assertEqual(len(self._notifications(self.courier)), 1)

    def test_pickup_task_titled_correctly(self):
        task = self._task(task_type="color_lab_pickup")
        task.courier = self.courier
        task.save()

        self.assertIn("Забор для Color Lab", self._notifications(self.courier)[0].title)

    # ── Движение по задаче ───────────────────────────────────────────────────

    def test_client_notified_when_courier_starts(self):
        task = self._task(courier=self.courier, status="assigned")
        Notification.objects.all().delete()

        task.status = "in_progress"
        task.save()

        notes = self._notifications(self.client_user)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].title, "Курьер в пути")
        self.assertIn("10:00 - 18:00", notes[0].body)

    def test_client_notified_on_delivery(self):
        task = self._task(courier=self.courier, status="in_progress")
        Notification.objects.all().delete()

        task.status = "delivered"
        task.save()

        self.assertEqual(self._notifications(self.client_user)[0].title, "Заказ доставлен")

    def test_pickup_completion_worded_as_pickup(self):
        task = self._task(task_type="color_lab_pickup", courier=self.courier, status="in_progress")
        Notification.objects.all().delete()

        task.status = "delivered"
        task.save()

        self.assertEqual(self._notifications(self.client_user)[0].title, "Курьер забрал лючок")

    def test_cancelled_status_sends_nothing(self):
        task = self._task(courier=self.courier, status="assigned")
        Notification.objects.all().delete()

        task.status = "cancelled"
        task.save()

        self.assertEqual(self._notifications(self.client_user), [])

    # ── Эксперт ──────────────────────────────────────────────────────────────

    def _expert(self, username, role):
        user = User.objects.create_user(username=username, password="pw")
        user.profile.role = role
        user.profile.save()
        return user

    def test_experts_notified_on_new_ticket(self):
        expert = self._expert("expert1", "ai_expert")

        ExpertTicket.objects.create(
            client=self.client_profile, question="Шагрень при покраске капота",
            category="Покраска", risk="high",
        )

        notes = self._notifications(expert)
        self.assertEqual(len(notes), 1)
        self.assertIn("Покраска", notes[0].title)
        self.assertIn("⚡", notes[0].title)  # высокий риск виден сразу
        self.assertIn("Автосервис", notes[0].body)
        self.assertIn("Шагрень", notes[0].body)
        self.assertEqual(notes[0].type, "action_required")

    def test_legacy_expert_role_spelling_also_notified(self):
        # seed_db пишет роль как "expert", хотя в Profile.Role она "ai_expert".
        legacy = self._expert("expert_legacy", "expert")

        ExpertTicket.objects.create(
            client=self.client_profile, question="Вопрос", category="Общее",
        )

        self.assertEqual(len(self._notifications(legacy)), 1)

    def test_all_experts_get_the_ticket(self):
        first = self._expert("expert1", "ai_expert")
        second = self._expert("expert2", "ai_expert")

        ExpertTicket.objects.create(
            client=self.client_profile, question="Вопрос", category="Общее",
        )

        self.assertEqual(len(self._notifications(first)), 1)
        self.assertEqual(len(self._notifications(second)), 1)

    def test_client_not_notified_of_own_question(self):
        self._expert("expert1", "ai_expert")
        ExpertTicket.objects.create(
            client=self.client_profile, question="Вопрос", category="Общее",
        )
        self.assertEqual(self._notifications(self.client_user), [])

    def test_long_question_truncated(self):
        expert = self._expert("expert1", "ai_expert")
        ExpertTicket.objects.create(
            client=self.client_profile, question="ф" * 300, category="Общее",
        )

        body = self._notifications(expert)[0].body
        self.assertTrue(body.endswith("…"))
        self.assertLess(len(body), 200)

    def test_client_notified_when_expert_answers(self):
        expert = self._expert("expert1", "ai_expert")
        ticket = ExpertTicket.objects.create(
            client=self.client_profile, question="Вопрос", category="Покраска",
        )
        Notification.objects.all().delete()

        ticket.expert_answer = "Нужно снизить давление на краскопульте."
        ticket.expert_author = expert
        ticket.status = "expertAnswered"
        ticket.save()

        notes = self._notifications(self.client_user)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].title, "Эксперт ответил на ваш вопрос")
        self.assertIn("краскопульте", notes[0].body)
        self.assertEqual(notes[0].related_link, "/qa")

    def test_no_duplicate_when_answered_ticket_saved_again(self):
        expert = self._expert("expert1", "ai_expert")
        ticket = ExpertTicket.objects.create(
            client=self.client_profile, question="Вопрос", category="Общее",
        )
        ticket.expert_answer = "Ответ"
        ticket.expert_author = expert
        ticket.status = "expertAnswered"
        ticket.save()
        Notification.objects.filter(user=self.client_user).delete()

        ticket.expert_answer = "Ответ, дополнен"
        ticket.save()

        self.assertEqual(self._notifications(self.client_user), [])

    def test_ticket_without_experts_does_not_crash(self):
        ticket = ExpertTicket.objects.create(
            client=self.client_profile, question="Вопрос", category="Общее",
        )
        self.assertIsNotNone(ticket.pk)

    def test_push_failure_does_not_break_save(self):
        self._expert("expert1", "ai_expert")
        with patch(
            "api.services.push_notifications.PushNotificationService.send",
            side_effect=RuntimeError("FCM недоступен"),
        ):
            ticket = ExpertTicket.objects.create(
                client=self.client_profile, question="Вопрос", category="Общее",
            )
        self.assertIsNotNone(ticket.pk)


class ColorRequestNotificationRegressionTests(TestCase):
    """Существующие уведомления по колеровке не должны сломаться."""

    def setUp(self):
        patcher = patch("api.services.push_notifications.PushNotificationService.send",
                        return_value={"sent": 0, "failed": 0})
        patcher.start()
        self.addCleanup(patcher.stop)

        distributor = Distributor.objects.create(
            name="Dist", inn="1112223334", phone="1", email="d@e.co"
        )
        region = Region.objects.create(code="77", name="Msk", distributor=distributor)
        self.user = User.objects.create_user(username="+79001110000", password="pw")
        self.client_profile = ClientProfile.objects.create(
            user=self.user, inn="5556667778", company_name="Автосервис",
            contact_name="Иван", phone="+79001110000",
            region=region, city="Москва", distributor=distributor,
        )

    def test_color_request_ready_still_notifies(self):
        item = ColorRequest.objects.create(
            client=self.client_profile, car_brand="Toyota", car_model="Camry",
            vin="XW8", color_code="1F7", transfer_method="self_delivery",
        )
        Notification.objects.all().delete()

        item.status = "ready"
        item.save()

        notes = Notification.objects.filter(user=self.user, type="color")
        self.assertEqual(notes.count(), 1)
        self.assertEqual(notes.first().title, "Подбор цвета завершён")
