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


class ColorRequestSimplifiedFormTests(ColorRequestPaintTypeTests):
    """Форма подбора: остались марка, код и тип покрытия.

    Модель, VIN и год из подбора убраны — цвет определяют марка и код, а
    лишние поля только удлиняли форму. В базе они сохранены: в старых заявках
    заполнены, и терять эти данные нельзя.
    """

    def _minimal(self, **extra):
        payload = {
            "carBrand": "Toyota",
            "colorCode": "1F7",
            "paintType": "baseClear",
            "transferMethod": "self_delivery",
        }
        payload.update(extra)
        return payload

    def test_request_is_created_without_model_vin_and_year(self):
        response = self._post("/api/color-requests/create/", self._minimal())

        self.assertEqual(response.status_code, 201, response.content)
        item = ColorRequest.objects.get()
        self.assertEqual(item.car_brand, "Toyota")
        self.assertEqual(item.car_model, "")
        self.assertEqual(item.vin, "")
        self.assertEqual(item.car_year, "")

    def test_paint_type_note_is_saved(self):
        response = self._post(
            "/api/color-requests/create/",
            self._minimal(paintTypeNote="Перламутр в 3 слоя, матовый лак"),
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(
            ColorRequest.objects.get().paint_type_note,
            "Перламутр в 3 слоя, матовый лак",
        )

    def test_note_is_returned_to_the_app(self):
        self._post("/api/color-requests/create/", self._minimal(paintTypeNote="Матовый"))

        data = self.http.get(
            "/api/color-requests/", HTTP_AUTHORIZATION=f"Bearer {self.token.key}"
        ).json()

        self.assertEqual(data["results"][0]["paintTypeNote"], "Матовый")

    def test_empty_note_comes_back_as_null(self):
        # Пустую строку приложение печатало бы как « · » рядом с типом.
        self._post("/api/color-requests/create/", self._minimal())

        data = self.http.get(
            "/api/color-requests/", HTTP_AUTHORIZATION=f"Bearer {self.token.key}"
        ).json()

        self.assertIsNone(data["results"][0]["paintTypeNote"])

    def test_note_can_be_edited(self):
        self._post("/api/color-requests/create/", self._minimal())
        item = ColorRequest.objects.get()

        self._post(
            f"/api/color-requests/{item.id}/update/",
            {"paintTypeNote": "Уточнил: двухслойный перламутр"},
        )

        item.refresh_from_db()
        self.assertEqual(item.paint_type_note, "Уточнил: двухслойный перламутр")

    def test_old_request_keeps_its_model_and_vin(self):
        """Данные прежних заявок не должны потеряться."""
        old = ColorRequest.objects.create(
            client=self.client_profile, car_brand="BMW", car_model="X5",
            car_year="2019", vin="WBA123", color_code="A96", paint_type="acrylic",
        )

        old.refresh_from_db()
        self.assertEqual(old.car_model, "X5")
        self.assertEqual(old.vin, "WBA123")

    def test_all_three_coating_types_are_accepted(self):
        for index, code in enumerate(["acrylic", "baseClear", "threeStage"]):
            response = self._post(
                "/api/color-requests/create/",
                self._minimal(paintType=code, colorCode=f"C-{index}"),
            )
            self.assertEqual(response.status_code, 201, f"{code}: {response.content}")

        self.assertEqual(ColorRequest.objects.count(), 3)
