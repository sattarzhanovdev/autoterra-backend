"""Скидки по рангу клиента.

Гаражный сервис и дилерский салон закупают по-разному, поэтому платят по
разной цене за один и тот же товар. Правила задаёт админка (RankDiscount),
цена считается в одном месте — api.services.pricing.
"""

from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext

from .models import (
    AuthToken,
    ClientProfile,
    Distributor,
    Order,
    PartnerTier,
    Product,
    RankDiscount,
    Region,
)
from .services.pricing import apply_discount, discount_percent, price_for_client


class RankDiscountTests(TestCase):
    def setUp(self):
        self.http = Client()
        self.distributor = Distributor.objects.create(
            name="Dist", inn="1112223334", phone="1", email="d@e.co"
        )
        self.other_distributor = Distributor.objects.create(
            name="Other", inn="9998887776", phone="2", email="o@e.co"
        )
        self.region = Region.objects.create(code="77", name="Msk", distributor=self.distributor)

        # Ранги посеяны миграцией; берём крайние ступени лестницы.
        self.base = PartnerTier.objects.get(name="Базовый")
        self.platinum = PartnerTier.objects.get(name="Platinum")

        self.paint = Product.objects.create(
            distributor=self.distributor, sku="P-1", name="Краска базовая",
            category="Краски", price=Decimal("10000"), quantity=100,
        )
        self.tool = Product.objects.create(
            distributor=self.distributor, sku="T-1", name="Краскопульт",
            category="Инструмент", price=Decimal("20000"), quantity=100,
        )

    _inn_counter = 0

    def _client(self, username, tier):
        RankDiscountTests._inn_counter += 1
        user = User.objects.create_user(username=username, password="pw")
        profile = ClientProfile.objects.create(
            user=user, inn=f"{5000000000 + self._inn_counter}",
            company_name=f"Компания {username}", partner_status=tier,
            contact_name="Иван", phone="+79001110000",
            region=self.region, city="Москва", distributor=self.distributor,
        )
        token = AuthToken.objects.create(key=f"token-{username}", user=user)
        return profile, token

    # ── Расчёт ───────────────────────────────────────────────────────────────

    def test_no_rules_means_base_price(self):
        garage, _ = self._client("garage", "Базовый")
        self.assertEqual(price_for_client(garage, self.paint), Decimal("10000.00"))

    def test_salon_pays_less_than_garage(self):
        RankDiscount.objects.create(tier=self.platinum, percent=Decimal("20"))
        RankDiscount.objects.create(tier=self.base, percent=Decimal("5"))

        salon, _ = self._client("salon", "Platinum")
        garage, _ = self._client("garage", "Базовый")

        self.assertEqual(price_for_client(salon, self.paint), Decimal("8000.00"))
        self.assertEqual(price_for_client(garage, self.paint), Decimal("9500.00"))

    def test_category_rule_beats_catalog_wide_rule(self):
        RankDiscount.objects.create(tier=self.platinum, percent=Decimal("10"))
        RankDiscount.objects.create(tier=self.platinum, product_category="Краски", percent=Decimal("25"))

        salon, _ = self._client("salon", "Platinum")
        self.assertEqual(price_for_client(salon, self.paint), Decimal("7500.00"))  # 25% на краски
        self.assertEqual(price_for_client(salon, self.tool), Decimal("18000.00"))  # 10% на остальное

    def test_distributor_rule_beats_global_rule(self):
        RankDiscount.objects.create(tier=self.platinum, percent=Decimal("10"))
        RankDiscount.objects.create(
            tier=self.platinum, distributor=self.distributor, percent=Decimal("30")
        )

        salon, _ = self._client("salon", "Platinum")
        self.assertEqual(price_for_client(salon, self.paint), Decimal("7000.00"))

    def test_rule_of_another_distributor_ignored(self):
        RankDiscount.objects.create(
            tier=self.platinum, distributor=self.other_distributor, percent=Decimal("50")
        )
        salon, _ = self._client("salon", "Platinum")
        self.assertEqual(price_for_client(salon, self.paint), Decimal("10000.00"))

    def test_inactive_rule_ignored(self):
        RankDiscount.objects.create(tier=self.platinum, percent=Decimal("20"), is_active=False)
        salon, _ = self._client("salon", "Platinum")
        self.assertEqual(price_for_client(salon, self.paint), Decimal("10000.00"))

    def test_rule_of_another_rank_ignored(self):
        RankDiscount.objects.create(tier=self.platinum, percent=Decimal("20"))
        garage, _ = self._client("garage", "Базовый")
        self.assertEqual(price_for_client(garage, self.paint), Decimal("10000.00"))

    def test_rounding_to_kopecks(self):
        self.assertEqual(apply_discount(Decimal("999.99"), Decimal("7.5")), Decimal("924.99"))

    def test_full_discount_is_free_not_negative(self):
        self.assertEqual(apply_discount(Decimal("100"), Decimal("100")), Decimal("0.00"))

    def test_percent_lookup_without_client(self):
        self.assertEqual(discount_percent(None, self.paint), Decimal("0"))

    # ── Витрина ──────────────────────────────────────────────────────────────

    def test_catalog_shows_rank_price_and_base(self):
        RankDiscount.objects.create(tier=self.platinum, percent=Decimal("20"))
        _, token = self._client("salon", "Platinum")

        response = self.http.get(
            "/api/products/", HTTP_AUTHORIZATION=f"Bearer {token.key}"
        )
        self.assertEqual(response.status_code, 200, response.content)
        paint = next(p for p in response.json()["results"] if p["sku"] == "P-1")

        self.assertEqual(paint["price"], 8000.0)
        self.assertEqual(paint["basePrice"], 10000.0)
        self.assertEqual(paint["discountPercent"], 20.0)
        self.assertTrue(paint["hasDiscount"])

    def test_catalog_without_rules_has_no_discount_flag(self):
        _, token = self._client("garage", "Базовый")
        response = self.http.get("/api/products/", HTTP_AUTHORIZATION=f"Bearer {token.key}")
        paint = next(p for p in response.json()["results"] if p["sku"] == "P-1")

        self.assertEqual(paint["price"], 10000.0)
        self.assertFalse(paint["hasDiscount"])

    def test_catalog_price_query_count_does_not_grow(self):
        """Правила читаются раз на запрос, а не на каждый товар (N+1)."""
        RankDiscount.objects.create(tier=self.platinum, percent=Decimal("20"))
        _, token = self._client("salon", "Platinum")

        def fetch_query_count(page_size):
            with CaptureQueriesContext(connection) as ctx:
                response = self.http.get(
                    f"/api/products/?pageSize={page_size}",
                    HTTP_AUTHORIZATION=f"Bearer {token.key}",
                )
                self.assertEqual(response.status_code, 200, response.content)
            return len(ctx.captured_queries)

        baseline = fetch_query_count(50)

        for i in range(30):
            Product.objects.create(
                distributor=self.distributor, sku=f"X-{i}", name=f"Товар {i}",
                category="Краски", price=Decimal("1000"), quantity=5,
            )

        self.assertEqual(fetch_query_count(50), baseline)

    # ── Заказ ────────────────────────────────────────────────────────────────

    def test_order_locks_in_rank_price(self):
        RankDiscount.objects.create(tier=self.platinum, percent=Decimal("20"))
        _, token = self._client("salon", "Platinum")

        response = self.http.post(
            "/api/orders/create/",
            data={"items": [{"productId": str(self.paint.id), "quantity": 2}]},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token.key}",
        )
        self.assertEqual(response.status_code, 201, response.content)

        order = Order.objects.get(id=response.json()["order"]["id"])
        item = order.items.first()
        self.assertEqual(item.price, Decimal("8000.00"))
        self.assertEqual(item.total, Decimal("16000.00"))

    def test_two_ranks_pay_differently_for_same_product(self):
        RankDiscount.objects.create(tier=self.platinum, percent=Decimal("20"))
        RankDiscount.objects.create(tier=self.base, percent=Decimal("5"))
        _, salon_token = self._client("salon", "Platinum")
        _, garage_token = self._client("garage", "Базовый")

        totals = []
        for token in (salon_token, garage_token):
            response = self.http.post(
                "/api/orders/create/",
                data={"items": [{"productId": str(self.paint.id), "quantity": 1}]},
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {token.key}",
            )
            self.assertEqual(response.status_code, 201, response.content)
            totals.append(Decimal(str(response.json()["order"]["totalAmount"])))

        self.assertEqual(totals, [Decimal("8000"), Decimal("9500")])

    def test_price_change_after_order_does_not_touch_it(self):
        RankDiscount.objects.create(tier=self.platinum, percent=Decimal("20"))
        _, token = self._client("salon", "Platinum")
        response = self.http.post(
            "/api/orders/create/",
            data={"items": [{"productId": str(self.paint.id), "quantity": 1}]},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token.key}",
        )
        order_id = response.json()["order"]["id"]

        RankDiscount.objects.all().update(percent=Decimal("50"))

        item = Order.objects.get(id=order_id).items.first()
        self.assertEqual(item.price, Decimal("8000.00"))
