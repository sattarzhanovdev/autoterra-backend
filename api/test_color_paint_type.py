"""Тип покрытия в заявке на подбор цвета.

Клиент добирает краску в тот же цвет, а цена у акрила, базы под лак и
трёхстадийной разная — поэтому тип выбирается в заявке и приходит в API.
"""

from django.contrib.auth.models import User
from django.test import Client, TestCase

from .models import AuthToken, ClientProfile, ColorRequest, Distributor, Region


class ColorRequestPaintTypeTests(TestCase):
    def setUp(self):
        self.http = Client()
        distributor_user = User.objects.create_user(username="dist-paint", password="pw")
        distributor_user.profile.role = "distributor"
        distributor_user.profile.save()
        self.distributor = Distributor.objects.create(
            user=distributor_user, name="Dist", inn="2223334445", phone="1", email="d@e.co"
        )
        self.region = Region.objects.create(code="78", name="Spb", distributor=self.distributor)

        client_user = User.objects.create_user(username="+79005550000", password="pw")
        self.client_profile = ClientProfile.objects.create(
            user=client_user, inn="6667778889", company_name="Автосервис",
            contact_name="Иван", phone="+79005550000",
            region=self.region, city="Санкт-Петербург", distributor=self.distributor,
        )
        self.token = AuthToken.objects.create(key="paint-token", user=client_user)

    def _post(self, path, payload):
        return self.http.post(
            path,
            data=payload,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.key}",
        )

    def _payload(self, **extra):
        payload = {
            "carBrand": "Toyota", "carModel": "Camry", "vin": "XW8",
            "colorCode": "1F7", "transferMethod": "self_delivery",
            "paintType": "threeStage",
        }
        payload.update(extra)
        return payload

    def test_paint_type_saved_and_returned(self):
        response = self._post("/api/color-requests/create/", self._payload())
        self.assertEqual(response.status_code, 201, response.content)

        created = response.json()["request"]
        self.assertEqual(created["paintType"], "threeStage")
        self.assertEqual(created["paintTypeLabel"], "Трёхстадийная")
        self.assertEqual(ColorRequest.objects.get(id=created["id"]).paint_type, "threeStage")

    def test_paint_type_required(self):
        payload = self._payload()
        payload.pop("paintType")
        response = self._post("/api/color-requests/create/", payload)
        # Молча подставить умолчание нельзя: клиент получил бы чужую цену.
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(ColorRequest.objects.exists())

    def test_unknown_paint_type_rejected(self):
        response = self._post("/api/color-requests/create/", self._payload(paintType="metallic"))
        self.assertEqual(response.status_code, 400, response.content)

    def test_paint_type_can_be_changed_until_in_progress(self):
        created = self._post("/api/color-requests/create/", self._payload()).json()["request"]

        response = self._post(
            f"/api/color-requests/{created['id']}/update/", {"paintType": "acrylic"}
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["request"]["paintType"], "acrylic")
        self.assertEqual(ColorRequest.objects.get(id=created["id"]).paint_type, "acrylic")
