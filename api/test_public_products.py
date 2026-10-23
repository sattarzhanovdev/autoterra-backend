"""Public catalog access must preserve client scoping and pricing."""

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from .models import AuthToken, ClientPriceOverride, ClientProfile, Distributor, Product, Region


@override_settings(ELASTICSEARCH_URL="")
class PublicProductsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        region = Region.objects.create(code="CAT", name="Catalog")
        cls.distributor = Distributor.objects.create(name="First", inn="7701000001")
        other = Distributor.objects.create(name="Other", inn="7701000002")
        inactive = Distributor.objects.create(name="Inactive", inn="7701000003", is_active=False)
        cls.user = User.objects.create_user(username="catalog-client")
        cls.user.profile.role = "client"
        cls.user.profile.save()
        cls.profile = ClientProfile.objects.create(
            user=cls.user, inn="1234567890", company_name="Catalog client",
            region=region, distributor=cls.distributor, status="active",
            city="Бишкек", contact_name="Клиент", phone="+996700000001",
            personal_discount_percent=20,
        )
        cls.token = AuthToken.objects.create(key="catalog-token", user=cls.user).key
        cls.primer = Product.objects.create(
            distributor=cls.distributor, sku="PRIMER", name="Грунт акриловый",
            category="Грунты", brand="First", price=100, quantity=5,
        )
        cls.lacquer = Product.objects.create(
            distributor=other, sku="LACQUER", name="Лак прозрачный",
            category="Лаки", brand="Other", price=200, quantity=0, status="onOrder",
        )
        cls.empty = Product.objects.create(
            distributor=other, sku="EMPTY", name="Краска", category="Краски",
            brand="Other", price=300, quantity=0, status="outOfStock",
        )
        Product.objects.create(
            distributor=cls.distributor, sku="HIDDEN", name="Грунт скрытый",
            category="Скрытые товары", brand="Hidden", price=400, is_active=False,
        )
        Product.objects.create(
            distributor=inactive, sku="INACTIVE", name="Грунт недоступный",
            category="Неактивный дистрибьютор", brand="Hidden", price=500,
        )

    def catalog(self, params=None, authorization=None):
        headers = {} if authorization is None else {"HTTP_AUTHORIZATION": authorization}
        response = self.client.get("/api/products/", params or {}, **headers)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_public_catalog_scope_base_prices_and_response_shape(self):
        body = self.catalog()
        self.assertEqual(set(body), {"results", "pagination", "count", "categories"})
        self.assertEqual(body["count"], 3)
        self.assertEqual({row["id"] for row in body["results"]},
                         {str(self.primer.pk), str(self.lacquer.pk), str(self.empty.pk)})
        self.assertEqual(set(body["categories"]), {"Грунты", "Лаки", "Краски"})
        self.assertEqual({row["id"]: row["price"] for row in body["results"]},
                         {str(self.primer.pk): 100, str(self.lacquer.pk): 200, str(self.empty.pk): 300})
        for row in body["results"]:
            self.assertNotIn("personalPrice", row)
            self.assertNotIn("discountPercent", row)

    def test_public_filters_keep_catalog_scope_and_categories(self):
        for params, expected in (
            ({"category": "Лаки"}, [self.lacquer]),
            ({"brand": "Other"}, [self.lacquer, self.empty]),
            ({"search": "грунт"}, [self.primer]),
            ({"inStock": "true"}, [self.primer, self.lacquer]),
            ({"brand": "Other", "inStock": "true", "page_size": 1}, [self.lacquer]),
            ({"category": "Скрытые товары"}, []),
            ({"brand": "Hidden"}, []),
        ):
            with self.subTest(params=params):
                body = self.catalog(params)
                self.assertEqual({row["id"] for row in body["results"]}, {str(p.pk) for p in expected})
                self.assertEqual(body["count"], len(expected))
                self.assertEqual(set(body["categories"]), {"Грунты", "Лаки", "Краски"})

    def test_public_pagination(self):
        body = self.catalog({"page": 1, "page_size": 20})
        self.assertEqual(body["pagination"]["page"], 1)
        self.assertEqual(body["pagination"]["pageSize"], 20)
        first = self.catalog({"page": 1, "page_size": 2})
        second = self.catalog({"page": 2, "page_size": 2})
        self.assertEqual(len(first["results"]), 2)
        self.assertEqual(len(second["results"]), 1)
        self.assertTrue(first["pagination"]["hasNext"])
        self.assertFalse(second["pagination"]["hasNext"])
        self.assertEqual(len({row["id"] for row in first["results"] + second["results"]}), 3)

    def test_authenticated_client_scope_and_discount(self):
        body = self.catalog(authorization=f"Bearer {self.token}")
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["categories"], ["Грунты"])
        row = body["results"][0]
        self.assertEqual(row["id"], str(self.primer.pk))
        self.assertEqual(row["price"], 80)
        self.assertEqual(row["basePrice"], 100)
        self.assertEqual(row["discountPercent"], 20)
        self.assertTrue(row["hasDiscount"])
        self.assertEqual(row["priceSource"], "personal_discount")

    def test_authenticated_client_personal_price(self):
        ClientPriceOverride.objects.create(client=self.profile, product=self.primer, price=65)
        row = self.catalog(authorization=f"Bearer {self.token}")["results"][0]
        self.assertEqual(row["price"], 65)
        self.assertEqual(row["personalPrice"], 65)
        self.assertEqual(row["priceSource"], "personal")

    def test_invalid_authorization_does_not_fall_back_to_public(self):
        for authorization in ("Bearer WRONG_TOKEN", "Bearer", "Basic invalid"):
            with self.subTest(authorization=authorization):
                response = self.client.get("/api/products/", HTTP_AUTHORIZATION=authorization)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json(), {"detail": "Unauthorized"})

    def test_blocked_client_does_not_fall_back_to_public(self):
        self.profile.status = "blocked"
        self.profile.save()
        response = self.client.get("/api/products/", HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(response.status_code, 403)

    def test_inactive_distributor_does_not_fall_back_to_public(self):
        self.distributor.is_active = False
        self.distributor.save()
        response = self.client.get("/api/products/", HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(response.status_code, 403)

    def test_other_role_does_not_fall_back_to_public(self):
        self.user.profile.role = "courier"
        self.user.profile.save()
        response = self.client.get("/api/products/", HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(response.status_code, 403)

    def test_order_endpoints_still_require_authorization(self):
        for path in ("/api/products/quote/?ids=1", "/api/order-config/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.post("/api/orders/create/", {}, content_type="application/json").status_code, 401)

    def test_openapi_catalog_has_optional_bearer(self):
        response = self.client.get("/api/schema/")
        self.assertEqual(response.status_code, 200)
        schema = response.json()
        self.assertEqual(schema["paths"]["/products/"]["get"]["security"], [{}, {"BearerAuth": []}])
        self.assertEqual(schema["paths"]["/auth/me/"]["get"]["security"], [{"BearerAuth": []}])
