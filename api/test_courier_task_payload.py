"""Что клиент получает по своим заявкам на доставку.

Карточка доставки в приложении показывает курьера, телефон, контакт и историю
статусов — если сервер их не отдаёт, клиенту нечем объяснить, где его заказ.
"""

from django.contrib.auth.models import User
from django.test import Client, TestCase

from .models import AuthToken, ClientProfile, CourierTask, Distributor, Region


class CourierTaskPayloadTests(TestCase):
    def setUp(self):
        self.http = Client()
        self.distributor = Distributor.objects.create(
            name="Dist", inn="1112223334", phone="1", email="d@e.co"
        )
        self.region = Region.objects.create(code="77", name="Msk", distributor=self.distributor)
        user = User.objects.create_user(username="+79001110000", password="pw")
        self.client_profile = ClientProfile.objects.create(
            user=user, inn="5556667778", company_name="Автосервис",
            contact_name="Иван Иванов", phone="+79001110000",
            region=self.region, city="Москва", distributor=self.distributor,
        )
        self.token = AuthToken.objects.create(key="client-token", user=user)

        courier_user = User.objects.create_user(username="+79002223344", password="pw")
        courier_user.first_name = "Пётр"
        courier_user.last_name = "Курьеров"
        courier_user.save()
        courier_user.profile.role = "courier"
        courier_user.profile.save()
        self.courier = courier_user

    def _task(self, **kwargs):
        defaults = dict(
            client=self.client_profile,
            task_type="delivery",
            address="Москва, пр. Мира 22",
            time_slot="10:00 - 18:00",
            contact_name="Иван Иванов",
            contact_phone="+79001110000",
            status="in_progress",
        )
        defaults.update(kwargs)
        return CourierTask.objects.create(**defaults)

    def _fetch(self):
        response = self.http.get(
            "/api/courier-tasks/",
            HTTP_AUTHORIZATION=f"Bearer {self.token.key}",
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["results"]

    def test_payload_has_contact_and_schedule(self):
        self._task()
        item = self._fetch()[0]
        self.assertEqual(item["contactName"], "Иван Иванов")
        self.assertEqual(item["contactPhone"], "+79001110000")
        self.assertEqual(item["address"], "Москва, пр. Мира 22")
        self.assertEqual(item["timeSlot"], "10:00 - 18:00")
        self.assertIn("scheduledTime", item)

    def test_courier_name_and_phone_exposed(self):
        self._task(courier=self.courier)
        item = self._fetch()[0]
        self.assertEqual(item["courierName"], "Пётр Курьеров")
        # Курьеры регистрируются по телефону — он же username.
        self.assertEqual(item["courierPhone"], "+79002223344")

    def test_no_courier_no_phone(self):
        self._task()
        item = self._fetch()[0]
        self.assertIsNone(item["courierName"])
        self.assertIsNone(item["courierPhone"])

    def test_service_login_is_not_shown_as_phone(self):
        staff = User.objects.create_user(username="courier_test", password="pw")
        self._task(courier=staff)
        item = self._fetch()[0]
        self.assertIsNone(item["courierPhone"])

    def test_status_history_reaches_client(self):
        task = self._task(status="assigned")
        task.status_history = [
            {"status": "created", "at": "2026-07-27T09:30:00+00:00", "by": None, "comment": ""},
            {"status": "assigned", "at": "2026-07-27T13:05:00+00:00", "by": None, "comment": ""},
        ]
        task.save(update_fields=["status_history"])

        item = self._fetch()[0]
        self.assertEqual([entry["status"] for entry in item["statusHistory"]], ["created", "assigned"])

    def test_created_task_records_history(self):
        response = self.http.post(
            "/api/courier-tasks/create/",
            data={"address": "Москва, Ленина 1", "contactName": "Пётр", "contactPhone": "+79005553322"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.key}",
        )
        self.assertEqual(response.status_code, 201, response.content)

        item = self._fetch()[0]
        self.assertEqual(item["contactName"], "Пётр")
        self.assertEqual(item["contactPhone"], "+79005553322")
        self.assertEqual([entry["status"] for entry in item["statusHistory"]], ["created"])
