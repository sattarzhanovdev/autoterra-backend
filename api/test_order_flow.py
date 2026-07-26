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

    # ── данные для экрана разбора заказа ────────────────────────────────────
    def test_order_item_exposes_id_and_stock(self):
        """Оператору нужны id позиции (для корректировки) и остаток."""
        order = self._make_order(qty=2)
        resp = self.http.get(f"/api/orders/{order.id}/", **self._dist())
        self.assertEqual(resp.status_code, 200, resp.content)

        item = resp.json()["order"]["items"][0]
        self.assertEqual(item["id"], str(order.items.first().id))
        self.assertEqual(item["productId"], str(self.product.id))
        self.assertEqual(item["availableQuantity"], 10)

    def test_item_id_is_accepted_by_adjust(self):
        """id из выдачи должен подходить для корректировки без преобразований."""
        order = self._make_order(qty=5)
        detail = self.http.get(f"/api/orders/{order.id}/", **self._dist()).json()
        item_id = detail["order"]["items"][0]["id"]

        resp = self._post(
            f"/api/orders/{order.id}/adjust/",
            self._dist(),
            {"items": [{"itemId": item_id, "quantity": 3}], "reason": "нет остатка"},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        order.refresh_from_db()
        self.assertEqual(order.status, "adjusted")
        self.assertEqual(order.items.first().quantity, 3)

    def test_on_order_product_has_no_stock_limit(self):
        """Товар под заказ остатком не ограничен — в выдаче это null."""
        self.product.status = "onOrder"
        self.product.quantity = 0
        self.product.save()
        order = self._make_order(qty=7)

        resp = self.http.get(f"/api/orders/{order.id}/", **self._dist())
        self.assertIsNone(resp.json()["order"]["items"][0]["availableQuantity"])

    # ── доступ к карточке заказа ────────────────────────────────────────────
    def test_client_can_open_own_order(self):
        order = self._make_order()
        resp = self.http.get(f"/api/orders/{order.id}/", **self._cli())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["order"]["id"], str(order.id))

    def test_foreign_client_cannot_open_order(self):
        other_user = User.objects.create_user(username="cli2", password="pw")
        other_user.profile.role = "client"
        other_user.profile.save()
        ClientProfile.objects.create(
            user=other_user, inn="9999999999", company_name="Чужой",
            region=self.region, distributor=self.distributor, phone="2",
            city="Msk", contact_name="Пётр",
        )
        token = AuthToken.objects.create(key="cli2-token", user=other_user)

        order = self._make_order()
        resp = self.http.get(
            f"/api/orders/{order.id}/", HTTP_AUTHORIZATION=f"Bearer {token.key}"
        )
        self.assertEqual(resp.status_code, 403)

    def test_order_detail_requires_auth(self):
        order = self._make_order()
        self.assertEqual(self.http.get(f"/api/orders/{order.id}/").status_code, 401)

    def test_order_detail_does_not_shadow_create(self):
        """`orders/create/` не должен перехватываться маршрутом карточки."""
        resp = self._post("/api/orders/create/", self._cli(), {"items": []})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("товары", resp.json()["detail"].lower())

    # ── редактирование состава оператором ───────────────────────────────────
    def test_operator_can_add_product_to_order(self):
        extra = Product.objects.create(
            distributor=self.distributor, sku="SKU2", name="Лак", category="Лаки",
            brand="AutoTerra", price=500, quantity=4, status="inStock",
        )
        order = self._make_order(qty=2)
        item = order.items.first()

        resp = self._post(
            f"/api/orders/{order.id}/adjust/", self._dist(),
            {
                "items": [{"itemId": item.id, "quantity": 2}],
                "newItems": [{"productId": extra.id, "quantity": 3}],
                "reason": "добавил замену",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        data = resp.json()["order"]
        skus = sorted(i["sku"] for i in data["items"])
        self.assertEqual(skus, ["SKU1", "SKU2"], "новая позиция должна быть в ответе")
        self.assertEqual(float(data["totalAmount"]), 2 * 1000 + 3 * 500)

        extra.refresh_from_db()
        self.assertEqual(extra.quantity, 1, "остаток добавленного товара должен списаться")

    def test_adjustment_snapshot_reflects_new_composition(self):
        order = self._make_order(qty=4)
        item = order.items.first()
        self._post(
            f"/api/orders/{order.id}/adjust/", self._dist(),
            {"items": [{"itemId": item.id, "quantity": 1}], "reason": "меньше"},
        )
        adjustment = order.adjustments.first()
        self.assertEqual(adjustment.original_items[0]["quantity"], 4)
        self.assertEqual(
            adjustment.adjusted_items[0]["quantity"], 1,
            "снимок «стало» должен быть после правок, а не из кэша prefetch",
        )

    def test_operator_can_remove_line_when_others_remain(self):
        extra = Product.objects.create(
            distributor=self.distributor, sku="SKU3", name="Растворитель",
            category="Растворители", brand="AutoTerra", price=300, quantity=9,
            status="inStock",
        )
        order = self._make_order(qty=2)
        OrderItem.objects.create(
            order=order, product=extra, sku=extra.sku, name=extra.name,
            category=extra.category, brand=extra.brand, price=extra.price, quantity=1,
        )
        first = order.items.get(sku="SKU1")

        resp = self._post(
            f"/api/orders/{order.id}/adjust/", self._dist(),
            {"items": [{"itemId": first.id, "quantity": 0}], "reason": "нет в наличии"},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual([i["sku"] for i in resp.json()["order"]["items"]], ["SKU3"])

    def test_adding_more_than_stock_is_rejected(self):
        extra = Product.objects.create(
            distributor=self.distributor, sku="SKU4", name="Отвердитель",
            category="Отвердители", brand="AutoTerra", price=700, quantity=2,
            status="inStock",
        )
        order = self._make_order(qty=1)
        resp = self._post(
            f"/api/orders/{order.id}/adjust/", self._dist(),
            {"newItems": [{"productId": extra.id, "quantity": 5}], "reason": "много"},
        )
        self.assertEqual(resp.status_code, 400)
        extra.refresh_from_db()
        self.assertEqual(extra.quantity, 2, "остаток не должен измениться при откате")

    def test_cannot_add_product_of_another_distributor(self):
        other = Distributor.objects.create(name="Other", inn="5556667778", phone="9", email="o@e.co")
        foreign = Product.objects.create(
            distributor=other, sku="SKU9", name="Чужой", category="Краски",
            brand="X", price=100, quantity=50, status="inStock",
        )
        order = self._make_order(qty=1)
        resp = self._post(
            f"/api/orders/{order.id}/adjust/", self._dist(),
            {"newItems": [{"productId": foreign.id, "quantity": 1}]},
        )
        self.assertEqual(resp.status_code, 400)
