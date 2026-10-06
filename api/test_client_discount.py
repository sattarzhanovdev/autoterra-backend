from decimal import Decimal
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from .models import AuthToken, AuditLog, ClientProfile, ClientPriceOverride, Order, PartnerTier, Product, RankDiscount
from .services.pricing import price_details, price_for_client
from .test_bonus_account import _Base


class ClientDiscountTests(_Base):
    def setUp(self):
        super().setUp()
        for role in ("manager", "admin", "distributor", "courier"):
            user = User.objects.create_user(username=role)
            user.profile.role = role
            user.profile.save()
            AuthToken.objects.create(user=user, key=role)
            if role == "manager":
                self.region.manager = user
                self.region.save()
            if role == "distributor":
                self.distributor.user = user
                self.distributor.save()
        self.client_profile.partner_status = "Platinum"
        self.client_profile.save()
        RankDiscount.objects.create(tier=PartnerTier.objects.get(name="Platinum"), percent=10)
        self.url = f"/api/clients/{self.client_profile.pk}/discount/"

    def auth(self, key="manager"):
        return {"HTTP_AUTHORIZATION": f"Bearer {key}"}

    def set_discount(self, value, key="manager"):
        return self.http.patch(self.url, {"personalDiscountPercent": value},
                               content_type="application/json", **self.auth(key))

    def fresh(self):
        return ClientProfile.objects.get(pk=self.client_profile.pk)

    def create_order(self):
        return self.http.post("/api/orders/create/", {"items": [
            {"productId": self.product.pk, "quantity": 2, "price": "0.01"}]},
            content_type="application/json", **self.auth(self.token))

    def test_twenty_and_fifteen_percent_catalog_and_server_order_prices(self):
        for percent, price in (("20", 800), ("15", 850)):
            with self.subTest(percent=percent):
                self.assertEqual(self.set_discount(percent).status_code, 200)
                row = self.http.get("/api/products/", **self.auth(self.token)).json()["results"][0]
                self.assertEqual(row["price"], price)
                self.assertEqual(row["basePrice"], 1000)
                self.assertEqual(row["discountPercent"], int(percent))
                self.assertEqual(row["priceSource"], "personal_discount")
                result = self.create_order()
                self.assertEqual(result.status_code, 201, result.content)
                order = Order.objects.get(pk=result.json()["order"]["id"])
                self.assertEqual(order.items.get().price, price)
                self.assertEqual(order.total_amount, price * 2)

    def test_all_products_and_other_clients(self):
        self.set_discount("20")
        other_product = Product.objects.create(distributor=self.distributor, sku="another", price="123.45")
        self.assertEqual(price_for_client(self.fresh(), other_product), Decimal("98.76"))
        other = self._client("second", "2223334445")
        other.personal_discount_percent = Decimal("15")
        other.save()
        self.assertEqual(price_for_client(other, self.product), Decimal("850"))

    def test_null_uses_rank_zero_overrides_rank_and_override_wins(self):
        self.assertEqual(price_for_client(self.fresh(), self.product), Decimal("900"))
        self.set_discount("0")
        self.assertEqual(price_for_client(self.fresh(), self.product), Decimal("1000"))
        self.set_discount("20")
        override = ClientPriceOverride.objects.create(client=self.client_profile, product=self.product, price=700)
        self.assertEqual(price_for_client(self.fresh(), self.product), Decimal("700"))
        override.is_active = False
        override.save()
        self.assertEqual(price_for_client(self.fresh(), self.product), Decimal("800"))
        self.set_discount(None)
        self.assertEqual(price_for_client(self.fresh(), self.product), Decimal("900"))
        RankDiscount.objects.all().delete()
        self.assertEqual(price_details(self.fresh(), self.product)["price_source"], "base")

    def test_existing_order_is_immutable_after_discount_change(self):
        self.set_discount("20")
        result = self.create_order()
        order = Order.objects.get(pk=result.json()["order"]["id"])
        self.set_discount("15")
        order.refresh_from_db()
        self.assertEqual(order.total_amount, Decimal("1600"))
        self.assertEqual(order.items.get().price, Decimal("800"))

    def test_manager_cannot_access_other_region_admin_can(self):
        self.region.manager = None
        self.region.save()
        self.assertEqual(self.set_discount("20").status_code, 404)
        self.assertEqual(self.http.get(self.url, **self.auth()).status_code, 404)
        self.assertEqual(self.set_discount("20", "admin").status_code, 200)
        self.assertEqual(self.fresh().personal_discount_percent, 20)

    def test_distributor_read_only_and_unrelated_roles_denied(self):
        self.set_discount("15")
        result = self.http.get(self.url, **self.auth("distributor"))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["personalDiscountPercent"], "15.00")
        self.assertFalse(result.json()["canEdit"])
        self.assertEqual(self.set_discount("20", "distributor").status_code, 403)
        stock = self.http.get("/api/distributor/stock/", {"clientId": self.client_profile.pk}, **self.auth("distributor"))
        self.assertEqual(stock.json()["results"][0]["price"], 850)
        for key in (self.token, "courier"):
            self.assertEqual(self.set_discount("20", key).status_code, 403)
        self.assertEqual(self.http.get(self.url).status_code, 401)

    def test_validation_and_database_constraints(self):
        self.set_discount("15")
        for value in (-1, 101, True, {}, [], "", "NaN", "Infinity", "1.001", "1e10000"):
            with self.subTest(value=value):
                self.assertEqual(self.set_discount(value).status_code, 400)
        self.assertEqual(self.fresh().personal_discount_percent, 15)
        for value in (-1, 101):
            client = self.fresh()
            client.personal_discount_percent = value
            with self.assertRaises(ValidationError):
                client.save()
            with self.assertRaises(IntegrityError), transaction.atomic():
                ClientProfile.objects.filter(pk=client.pk).update(personal_discount_percent=value)
        self.assertEqual(self.set_discount("100").status_code, 200)
        self.assertEqual(price_for_client(self.fresh(), self.product), 0)

    def test_audit_includes_actor_old_and_new_values_and_reset(self):
        self.set_discount("20")
        self.set_discount("15")
        self.set_discount(None)
        logs = list(AuditLog.objects.filter(action="Client discount updated").order_by("pk"))
        self.assertEqual(len(logs), 3)
        self.assertEqual(logs[1].user.username, "manager")
        self.assertEqual(logs[1].changes, {"oldPersonalDiscountPercent": "20.00", "personalDiscountPercent": "15.00"})
        self.assertIsNone(logs[2].changes["personalDiscountPercent"])

    def test_hundred_percent_order_and_payment(self):
        self.set_discount("100")
        result = self.create_order()
        self.assertEqual(result.status_code, 201, result.content)
        order = Order.objects.get(pk=result.json()["order"]["id"])
        order.status = "confirmed"
        order.save()
        from .views import _prepare_order_payment
        result = _prepare_order_payment(self.fresh(), order.pk, {})
        self.assertEqual(result.status_code, 201, result.content)
        order.refresh_from_db()
        self.assertEqual(order.status, "paid")
        self.assertEqual(order.payments.get().provider, "discount")
        self.assertEqual(order.payments.get().amount, 0)

    def test_empty_order_cannot_be_paid(self):
        from .views import _prepare_order_payment
        order = Order.objects.create(client=self.client_profile, distributor=self.distributor, status="confirmed")
        self.assertEqual(_prepare_order_payment(self.fresh(), order.pk, {}).status_code, 400)
        self.assertFalse(order.payments.exists())

    def test_django_admin_permissions_and_audit(self):
        from django.contrib import admin
        from django.test import RequestFactory
        from .admin import ClientProfileAdmin
        model_admin = ClientProfileAdmin(ClientProfile, admin.site)
        request = RequestFactory().get("/")
        request.user = User.objects.get(username="distributor")
        self.assertIn("personal_discount_percent", model_admin.get_readonly_fields(request, self.fresh()))
        request.user = User.objects.get(username="manager")
        self.assertNotIn("personal_discount_percent", model_admin.get_readonly_fields(request, self.fresh()))
        self.region.manager = None
        self.region.save()
        self.assertIn("personal_discount_percent", model_admin.get_readonly_fields(request, self.fresh()))
        request.user = User.objects.get(username="admin")
        client = self.fresh()
        client.personal_discount_percent = Decimal("20.00")
        model_admin.save_model(request, client, None, True)
        audit = AuditLog.objects.get(action="Client discount updated")
        self.assertEqual(audit.user, request.user)
        self.assertEqual(audit.changes["personalDiscountPercent"], "20.00")
