import json

from django.test import TestCase, Client
from django.contrib.auth.models import User

from .models import (
    AuthToken,
    ClientProfile,
    Distributor,
    Order,
    OrderItem,
    Payment,
    Product,
    Region,
)


class SafeOrderFlowTests(TestCase):
    def setUp(self):
        self.http = Client()

        # Дистрибьютор (оператор)
        self.dist_user = User.objects.create_user(username="dist", password="pw")
        self.dist_user.profile.role = "distributor"
        self.dist_user.profile.save()
        self.distributor = Distributor.objects.create(
            user=self.dist_user, name="Dist", inn="1112223334", phone="1", email="d@e.co"
        )
        self.region = Region.objects.create(code="77", name="Msk", distributor=self.distributor)
        self.dist_token = AuthToken.objects.create(key="dist-token", user=self.dist_user)

        # Клиент
        self.cli_user = User.objects.create_user(username="cli", password="pw", email="cli@e.co")
        self.cli_user.profile.role = "client"
        self.cli_user.profile.save()
        self.client_profile = ClientProfile.objects.create(
            user=self.cli_user, inn="1234567890", company_name="СТО Тест",
            region=self.region, distributor=self.distributor, phone="1",
            city="Msk", contact_name="Иван",
        )
        self.cli_token = AuthToken.objects.create(key="cli-token", user=self.cli_user)

        # Товар с остатком
        self.product = Product.objects.create(
            distributor=self.distributor, sku="SKU1", name="Грунт", category="Краски",
            brand="AutoTerra", price=1000, quantity=10, status="inStock",
        )

    # ── helpers ────────────────────────────────────────────────────────────────
    def _dist(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.dist_token.key}"}

    def _cli(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.cli_token.key}"}

    def _make_order(self, qty=2, status="new"):
        order = Order.objects.create(
            client=self.client_profile, distributor=self.distributor, status=status,
        )
        OrderItem.objects.create(
            order=order, product=self.product, sku=self.product.sku, name=self.product.name,
            category=self.product.category, brand=self.product.brand, price=self.product.price,
            quantity=qty,
        )
        return order

    def _post(self, url, headers, body=None):
        return self.http.post(
            url, data=json.dumps(body or {}), content_type="application/json", **headers
        )

    # ── state machine ────────────────────────────────────────────────────────────
    def test_cannot_pay_before_confirm(self):
        order = self._make_order()
        resp = self._post(f"/api/orders/{order.id}/pay/", self._cli())
        self.assertEqual(resp.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.status, "new")

    def test_operator_confirm_makes_order_payable(self):
        order = self._make_order()
        resp = self._post(f"/api/orders/{order.id}/confirm/", self._dist())
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["order"]
        self.assertEqual(data["status"], "confirmed")
        self.assertTrue(data["isPayable"])
        order.refresh_from_db()
        self.assertIsNotNone(order.confirmed_at)

    def test_confirm_blocked_by_stock_shortage(self):
        order = self._make_order(qty=2)
        self.product.quantity = 1
        self.product.save()
        resp = self._post(f"/api/orders/{order.id}/confirm/", self._dist())
        self.assertEqual(resp.status_code, 409)
        self.assertIn("shortages", resp.json())
        # force=1 обходит проверку
        resp2 = self._post(f"/api/orders/{order.id}/confirm/?force=1", self._dist())
        self.assertEqual(resp2.status_code, 200)

    def test_adjust_records_history_and_restores_stock(self):
        order = self._make_order(qty=4)
        self.product.quantity = 6  # 10 - 4 списанных зафиксируем вручную
        self.product.save()
        item = order.items.first()
        resp = self._post(
            f"/api/orders/{order.id}/adjust/", self._dist(),
            {"items": [{"itemId": item.id, "quantity": 1}], "reason": "Осталось меньше"},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["order"]
        self.assertEqual(data["status"], "adjusted")
        self.assertEqual(len(data["adjustments"]), 1)
        item.refresh_from_db()
        self.assertEqual(item.quantity, 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, 9)  # вернули 3 на склад (6+3)

    def test_client_accepts_adjustment_then_payable(self):
        order = self._make_order(status="adjusted")
        resp = self._post(f"/api/orders/{order.id}/accept-adjustment/", self._cli())
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["order"]["status"], "confirmed")

    def test_reject_restores_stock(self):
        order = self._make_order(qty=3)
        self.product.quantity = 7
        self.product.save()
        resp = self._post(
            f"/api/orders/{order.id}/reject/", self._dist(), {"reason": "Нет на складе"}
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        order.refresh_from_db()
        self.assertEqual(order.status, "rejected")
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, 10)  # 7 + 3 вернули

    def test_invalid_transition_blocked(self):
        order = self._make_order(status="fulfilled")
        resp = self._post(f"/api/orders/{order.id}/confirm/", self._dist())
        self.assertEqual(resp.status_code, 400)

    def test_pay_unconfigured_returns_503(self):
        order = self._make_order(status="confirmed")
        resp = self._post(f"/api/orders/{order.id}/pay/", self._cli())
        self.assertEqual(resp.status_code, 503)

    def test_webhook_marks_order_paid(self):
        order = self._make_order(status="confirmed")
        payment = Payment.objects.create(
            order=order, amount=order.total_amount, provider_payment_id="pay-123",
            status="pending",
        )
        body = {
            "event": "payment.succeeded",
            "object": {"id": "pay-123", "status": "succeeded", "metadata": {"order_id": str(order.id)}},
        }
        resp = self._post("/api/payments/yookassa/webhook/", {}, body)
        self.assertEqual(resp.status_code, 200)
        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(order.status, "paid")
        self.assertIsNotNone(order.paid_at)
        self.assertEqual(payment.status, "succeeded")

    def test_client_cancel_before_payment(self):
        order = self._make_order(qty=2, status="confirmed")
        self.product.quantity = 8
        self.product.save()
        resp = self._post(f"/api/orders/{order.id}/cancel/", self._cli())
        self.assertEqual(resp.status_code, 200, resp.content)
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, 10)
