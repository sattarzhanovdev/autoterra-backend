"""Проверка уведомлений по ходу заказа: in-app, push и письма."""

import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase, override_settings

from .models import (
    AuthToken,
    ClientProfile,
    Distributor,
    Notification,
    Order,
    OrderItem,
    Product,
    Region,
)


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    ORDER_NOTIFICATION_EMAILS=["operator@autoterra.ru"],
)
class OrderNotificationTests(TestCase):
    def setUp(self):
        self.http = Client()

        self.dist_user = User.objects.create_user(
            username="dist", password="pw", email="dist@autoterra.ru"
        )
        self.dist_user.profile.role = "distributor"
        self.dist_user.profile.save()
        self.distributor = Distributor.objects.create(
            user=self.dist_user, name="Dist", inn="1112223334", phone="1", email="d@e.co"
        )
        self.region = Region.objects.create(code="77", name="Msk", distributor=self.distributor)
        self.dist_token = AuthToken.objects.create(key="dist-token", user=self.dist_user)

        self.cli_user = User.objects.create_user(
            username="cli", password="pw", email="client@example.com"
        )
        self.cli_user.profile.role = "client"
        self.cli_user.profile.save()
        self.client_profile = ClientProfile.objects.create(
            user=self.cli_user, inn="1234567890", company_name="СТО Тест",
            region=self.region, distributor=self.distributor, phone="1",
            city="Msk", contact_name="Иван",
        )
        self.cli_token = AuthToken.objects.create(key="cli-token", user=self.cli_user)

        self.product = Product.objects.create(
            distributor=self.distributor, sku="SKU1", name="Грунт", category="Краски",
            brand="AutoTerra", price=1000, quantity=10, status="inStock",
        )

    def _dist(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.dist_token.key}"}

    def _cli(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.cli_token.key}"}

    def _post(self, url, headers, body=None):
        return self.http.post(
            url, data=json.dumps(body or {}), content_type="application/json", **headers
        )

    def _make_order(self, qty=2, status="new"):
        order = Order.objects.create(
            client=self.client_profile, distributor=self.distributor, status=status
        )
        OrderItem.objects.create(
            order=order, product=self.product, sku=self.product.sku, name=self.product.name,
            category=self.product.category, brand=self.product.brand,
            price=self.product.price, quantity=qty,
        )
        return order

    def _notes(self, user):
        return Notification.objects.filter(user=user).order_by("-id")

    # ── кто получает уведомление на каждом шаге ─────────────────────────────
    def test_new_order_notifies_operator(self):
        resp = self._post(
            "/api/orders/create/", self._cli(),
            {"items": [{"productId": str(self.product.id), "quantity": 1}], "comment": ""},
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(self._notes(self.dist_user).count(), 1)
        self.assertIn("Новый заказ", self._notes(self.dist_user).first().title)

    def test_confirm_notifies_client_with_deep_link(self):
        order = self._make_order()
        self._post(f"/api/orders/{order.id}/confirm/", self._dist())

        note = self._notes(self.cli_user).first()
        self.assertIsNotNone(note)
        self.assertEqual(note.type, "order")
        # Ссылка ведёт на карточку заказа — её открывает push-обработчик.
        self.assertEqual(note.related_link, f"/orders/{order.id}")

    def test_adjust_notifies_client(self):
        order = self._make_order(qty=4)
        item = order.items.first()
        self._post(
            f"/api/orders/{order.id}/adjust/", self._dist(),
            {"items": [{"itemId": item.id, "quantity": 1}], "reason": "нет остатка"},
        )
        note = self._notes(self.cli_user).first()
        self.assertIn("скорректирован", note.title.lower())

    def test_adjust_notification_carries_new_total(self):
        """Сумма в уведомлении должна быть уже пересчитанной."""
        order = self._make_order(qty=4)
        item = order.items.first()
        self._post(
            f"/api/orders/{order.id}/adjust/", self._dist(),
            {"items": [{"itemId": item.id, "quantity": 1}]},
        )
        note = self._notes(self.cli_user).first()
        self.assertIn("1000", note.body)
        self.assertNotIn("4000", note.body)

    def test_reject_notifies_client_with_reason(self):
        order = self._make_order()
        self._post(
            f"/api/orders/{order.id}/reject/", self._dist(), {"reason": "снят с производства"}
        )
        note = self._notes(self.cli_user).first()
        self.assertIn("снят с производства", note.body)

    def test_accept_adjustment_notifies_operator(self):
        order = self._make_order(status="adjusted")
        self._post(f"/api/orders/{order.id}/accept-adjustment/", self._cli())
        self.assertEqual(self._notes(self.dist_user).count(), 1)

    def test_ship_notifies_client(self):
        order = self._make_order(status="paid")
        self._post(f"/api/orders/{order.id}/ship/", self._dist())
        self.assertIn("отправлен", self._notes(self.cli_user).first().title.lower())

    def test_cancel_notifies_operator(self):
        order = self._make_order(status="confirmed")
        self._post(f"/api/orders/{order.id}/cancel/", self._cli())
        self.assertEqual(self._notes(self.dist_user).count(), 1)

    # ── письма ──────────────────────────────────────────────────────────────
    def test_confirm_emails_the_client(self):
        order = self._make_order()
        mail.outbox.clear()
        self._post(f"/api/orders/{order.id}/confirm/", self._dist())

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["client@example.com"])

    def test_reject_email_mentions_reason(self):
        order = self._make_order()
        mail.outbox.clear()
        self._post(f"/api/orders/{order.id}/reject/", self._dist(), {"reason": "брак партии"})

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("брак партии", mail.outbox[0].body)

    def test_client_without_email_does_not_break_confirm(self):
        self.cli_user.email = ""
        self.cli_user.save()
        order = self._make_order()

        resp = self._post(f"/api/orders/{order.id}/confirm/", self._dist())
        self.assertEqual(resp.status_code, 200, resp.content)
        # Письмо отправлять некуда, но in-app уведомление всё равно есть.
        self.assertEqual(self._notes(self.cli_user).count(), 1)

    # ── push ────────────────────────────────────────────────────────────────
    def test_push_is_attempted_for_each_notification(self):
        order = self._make_order()
        with patch("api.services.push_notifications.PushNotificationService.send") as send:
            self._post(f"/api/orders/{order.id}/confirm/", self._dist())
        send.assert_called_once()

    def test_push_failure_does_not_break_the_request(self):
        order = self._make_order()
        with patch(
            "api.services.push_notifications.PushNotificationService.send",
            side_effect=RuntimeError("FCM down"),
        ):
            resp = self._post(f"/api/orders/{order.id}/confirm/", self._dist())

        self.assertEqual(resp.status_code, 200, resp.content)
        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")

    # ── дистрибьютор без учётки ─────────────────────────────────────────────
    def test_order_flow_survives_distributor_without_user(self):
        """У дистрибьютора может не быть аккаунта — уведомлять некого."""
        self.distributor.user = None
        self.distributor.save()
        order = self._make_order(status="adjusted")

        resp = self._post(f"/api/orders/{order.id}/accept-adjustment/", self._cli())
        self.assertEqual(resp.status_code, 200, resp.content)
