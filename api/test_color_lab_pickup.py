"""Color Lab: курьер только забирает лючок, возврат — самовывоз.

Отзыв клиента: «Забрать лючок можем и будем, а вот отвозить нет, потому что
маляры смотрят оттенок на месте и бывает отдают на переделку». Плюс им нужен
дедлайн приезда курьера, чтобы понимать, до скольки его ждать.
"""

from datetime import time

from django.contrib.auth.models import User
from django.test import Client, TestCase

from .models import AuthToken, ClientProfile, ColorRequest, CourierTask, Distributor, Region


class ColorLabPickupTests(TestCase):
    def setUp(self):
        self.http = Client()
        distributor_user = User.objects.create_user(username="dist", password="pw")
        distributor_user.profile.role = "distributor"
        distributor_user.profile.save()
        self.distributor = Distributor.objects.create(
            user=distributor_user, name="Dist", inn="1112223334", phone="1", email="d@e.co"
        )
        self.region = Region.objects.create(code="77", name="Msk", distributor=self.distributor)
        self.distributor_token = AuthToken.objects.create(key="dist-token", user=distributor_user)

        client_user = User.objects.create_user(username="+79001110000", password="pw")
        self.client_profile = ClientProfile.objects.create(
            user=client_user, inn="5556667778", company_name="Автосервис",
            contact_name="Иван", phone="+79001110000",
            region=self.region, city="Москва", distributor=self.distributor,
        )
        self.client_token = AuthToken.objects.create(key="client-token", user=client_user)

    def _create_request(self, **extra):
        payload = {
            "carBrand": "Toyota", "carModel": "Camry", "vin": "XW8",
            "colorCode": "1F7", "transferMethod": "courier",
            "pickupAddress": "Москва, пр. Мира 22",
        }
        payload.update(extra)
        response = self.http.post(
            "/api/color-requests/create/",
            data=payload,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.client_token.key}",
        )
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["request"]

    def _set_status(self, request_id, status):
        return self.http.post(
            f"/api/distributor/color-requests/{request_id}/status/",
            data={"status": status, "recipe": "База 100 + пигмент 5"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.distributor_token.key}",
        )

    # ── Время приезда курьера ────────────────────────────────────────────────

    def test_arrive_until_saved_and_returned(self):
        created = self._create_request(courierArriveUntil="17:30")
        self.assertEqual(created["courierArriveUntil"], "17:30")

        item = ColorRequest.objects.get(id=created["id"])
        self.assertEqual(item.courier_arrive_until, time(17, 30))

    def test_arrive_until_optional(self):
        created = self._create_request()
        self.assertIsNone(created["courierArriveUntil"])

    def test_arrive_until_accepts_seconds_and_iso(self):
        self.assertEqual(self._create_request(courierArriveUntil="18:00:00")["courierArriveUntil"], "18:00")
        # Если клиент прислал дату-время целиком — берём из неё локальное время.
        self.assertEqual(
            self._create_request(courierArriveUntil="2026-07-28T19:15:00")["courierArriveUntil"],
            "19:15",
        )

    def test_arrive_until_editable_by_client(self):
        created = self._create_request(courierArriveUntil="17:30")
        response = self.http.post(
            f"/api/color-requests/{created['id']}/update/",
            data={"courierArriveUntil": "20:00"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.client_token.key}",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["request"]["courierArriveUntil"], "20:00")

    def test_garbage_time_is_ignored_not_crashing(self):
        created = self._create_request(courierArriveUntil="как получится")
        self.assertIsNone(created["courierArriveUntil"])

    # ── Забор лючка курьером остаётся ────────────────────────────────────────

    def test_courier_pickup_task_still_created(self):
        created = self._create_request()
        tasks = CourierTask.objects.filter(color_request_id=created["id"])
        self.assertEqual([t.task_type for t in tasks], ["color_lab_pickup"])

    def test_self_delivery_creates_no_task(self):
        created = self._create_request(transferMethod="self_delivery")
        self.assertFalse(CourierTask.objects.filter(color_request_id=created["id"]).exists())

    # ── Возврат курьером отключён ────────────────────────────────────────────

    def test_ready_does_not_create_return_task(self):
        created = self._create_request()
        response = self._set_status(created["id"], "ready")
        self.assertEqual(response.status_code, 200, response.content)

        tasks = CourierTask.objects.filter(color_request_id=created["id"])
        self.assertFalse(
            tasks.filter(task_type="return").exists(),
            "готовое маляр забирает сам — задача на возврат создаваться не должна",
        )
        self.assertEqual(tasks.count(), 1)  # только забор лючка

    def test_assigning_pickup_courier_does_not_close_request(self):
        created = self._create_request()
        self._set_status(created["id"], "ready")
        task = CourierTask.objects.get(color_request_id=created["id"])

        courier = User.objects.create_user(username="+79002223344", password="pw")
        response = self.http.post(
            f"/api/distributor/delivery-tasks/{task.id}/status/",
            data={"status": "assigned", "courierId": str(courier.id)},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.distributor_token.key}",
        )
        self.assertEqual(response.status_code, 200, response.content)

        item = ColorRequest.objects.get(id=created["id"])
        # Раньше назначение курьера помечало заявку «Выдана» — выдать может
        # только сам колорист, когда маляр приедет и проверит оттенок.
        self.assertEqual(item.status, "ready")
