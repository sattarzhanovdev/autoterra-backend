"""Удаление аккаунта — требование Apple 5.1.1(v) и Google Play.

Проверяем не факт «ручка ответила 200», а то, чем удаление отличается от
выхода из аккаунта: старый токен перестаёт работать, войти прежним паролем
нельзя, персональные данные в профиле затёрты, push-токены сняты. При этом
заказы остаются — это учётные документы, их удалять нельзя, и пользователю
мы обещаем ровно это.
"""

from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase

from .models import (
    AuthToken,
    ClientProfile,
    Distributor,
    Order,
    Region,
    UserDeviceToken,
)


class DeleteAccountTests(TestCase):
    def setUp(self):
        self.http = Client()
        self.distributor = Distributor.objects.create(
            name="Дист", inn="1112223334", phone="1", email="d@e.co"
        )
        self.region = Region.objects.create(
            code="77", name="Москва", distributor=self.distributor
        )
        self.user = User.objects.create_user(username="+79001110000", password="pw")
        self.user.profile.role = "client"
        self.user.profile.save()
        self.profile = ClientProfile.objects.create(
            user=self.user,
            inn="5556667778",
            company_name="ООО Ромашка",
            contact_name="Иван Петров",
            phone="+79001110000",
            region=self.region,
            city="Москва",
            distributor=self.distributor,
            status="active",
        )
        self.order = Order.objects.create(
            client=self.profile, distributor=self.distributor, status="confirmed"
        )
        UserDeviceToken.objects.create(user=self.user, token="fcm-1", platform="android")
        self.token = AuthToken.objects.create(key="tok-1", user=self.user).key
        self.headers = {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}

    def test_requires_auth(self):
        resp = self.http.post("/api/auth/delete-account/")
        self.assertEqual(resp.status_code, 401)

    def test_deletes_account(self):
        resp = self.http.post("/api/auth/delete-account/", **self.headers)
        self.assertEqual(resp.status_code, 200)

        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertNotEqual(self.user.username, "+79001110000")

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.company_name, "Удалённый аккаунт")
        self.assertEqual(self.profile.contact_name, "Удалённый пользователь")
        self.assertEqual(self.profile.status, "archived")
        self.assertEqual(self.profile.inn, "0000000000")
        self.assertNotIn("79001110000", self.profile.phone)

        self.assertFalse(AuthToken.objects.filter(user=self.user).exists())
        self.assertFalse(UserDeviceToken.objects.filter(user=self.user).exists())

    def test_old_token_stops_working(self):
        self.http.post("/api/auth/delete-account/", **self.headers)
        resp = self.http.get("/api/auth/me/", **self.headers)
        self.assertEqual(resp.status_code, 401)

    def test_cannot_login_after_deletion(self):
        self.http.post("/api/auth/delete-account/", **self.headers)
        resp = self.http.post(
            "/api/login/",
            data={"phone": "+79001110000", "password": "pw"},
            content_type="application/json",
        )
        self.assertNotEqual(resp.status_code, 200)

    def test_orders_are_kept(self):
        """Первичные документы остаются — их хранение обязательно по закону."""
        self.http.post("/api/auth/delete-account/", **self.headers)
        self.assertTrue(Order.objects.filter(pk=self.order.pk).exists())
