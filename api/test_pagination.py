"""Тесты постраничной выдачи списочных эндпоинтов."""

from django.contrib.auth.models import User
from django.test import TestCase

from .models import AuthToken, ClientProfile, Distributor, Product, Region


class PaginationTests(TestCase):
    """Проверяем метаданные, границы страниц и совместимость со старым форматом."""

    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.create(code="MSK", name="Москва")
        cls.distributor = Distributor.objects.create(
            name="Дистрибьютор",
            inn="7701000001",
        )

        cls.user = User.objects.create_user(username="+79001112233", password="pass12345")
        cls.user.profile.role = "client"
        cls.user.profile.save()
        cls.client_profile = ClientProfile.objects.create(
            user=cls.user,
            inn="1234567890",
            company_name="ООО Тест",
            region=cls.region,
            distributor=cls.distributor,
            city="Москва",
            contact_name="Иван Иванов",
            phone="+79001112233",
        )
        cls.token = AuthToken.objects.create(key="client-token", user=cls.user).key

        # 25 товаров — больше одной страницы по умолчанию (20).
        Product.objects.bulk_create([
            Product(
                distributor=cls.distributor,
                sku=f"SKU-{i:03d}",
                name=f"Товар {i:03d}",
                category="Общее",
                brand="AutoTerra",
                price=100,
                quantity=5,
                is_active=True,
            )
            for i in range(25)
        ])

    def _get(self, path, **params):
        return self.client.get(
            path,
            params,
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )

    def test_first_page_has_default_size_and_metadata(self):
        response = self._get("/api/products/")
        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertEqual(len(body["results"]), 20)
        pagination = body["pagination"]
        self.assertEqual(pagination["page"], 1)
        self.assertEqual(pagination["pageSize"], 20)
        self.assertEqual(pagination["count"], 25)
        self.assertEqual(pagination["totalPages"], 2)
        self.assertTrue(pagination["hasNext"])
        self.assertFalse(pagination["hasPrevious"])

    def test_last_page_returns_remainder(self):
        body = self._get("/api/products/", page=2).json()

        self.assertEqual(len(body["results"]), 5)
        self.assertFalse(body["pagination"]["hasNext"])
        self.assertTrue(body["pagination"]["hasPrevious"])

    def test_pages_do_not_overlap(self):
        first = self._get("/api/products/", page=1).json()["results"]
        second = self._get("/api/products/", page=2).json()["results"]

        first_ids = {item["id"] for item in first}
        second_ids = {item["id"] for item in second}
        self.assertEqual(len(first_ids & second_ids), 0)
        self.assertEqual(len(first_ids | second_ids), 25)

    def test_custom_page_size(self):
        body = self._get("/api/products/", page_size=10).json()

        self.assertEqual(len(body["results"]), 10)
        self.assertEqual(body["pagination"]["totalPages"], 3)

    def test_page_size_is_capped(self):
        body = self._get("/api/products/", page_size=5000).json()

        # Верхняя граница — MAX_PAGE_SIZE, клиент не может выкачать всё разом.
        self.assertEqual(body["pagination"]["pageSize"], 100)

    def test_out_of_range_page_returns_last_page(self):
        body = self._get("/api/products/", page=999).json()

        self.assertEqual(body["pagination"]["page"], 2)
        self.assertFalse(body["pagination"]["hasNext"])

    def test_invalid_page_falls_back_to_first(self):
        body = self._get("/api/products/", page="abc").json()

        self.assertEqual(body["pagination"]["page"], 1)

    def test_legacy_limit_offset_still_works(self):
        body = self._get("/api/products/", limit=10, offset=20).json()

        self.assertEqual(len(body["results"]), 5)
        self.assertEqual(body["pagination"]["page"], 3)

    def test_search_filter_applies_before_pagination(self):
        body = self._get("/api/products/", search="Товар 001").json()

        self.assertEqual(body["pagination"]["count"], 1)
        self.assertEqual(len(body["results"]), 1)

    def test_extra_payload_survives_pagination(self):
        # У товаров рядом со списком приходит справочник категорий.
        body = self._get("/api/products/").json()

        self.assertIn("categories", body)
        self.assertEqual(body["categories"], ["Общее"])

    def test_empty_list_has_consistent_metadata(self):
        Product.objects.all().delete()
        body = self._get("/api/products/").json()

        self.assertEqual(body["results"], [])
        self.assertEqual(body["pagination"]["count"], 0)
        self.assertFalse(body["pagination"]["hasNext"])

    def test_order_config_no_longer_ships_whole_catalog(self):
        """Каталог уезжал целиком на каждое открытие экрана заказа."""
        body = self._get("/api/order-config/").json()

        self.assertNotIn("products", body)
        self.assertIn("categories", body)
        self.assertIn("brands", body)

    def test_in_stock_filter_excludes_zero_quantity(self):
        Product.objects.filter(sku="SKU-000").update(quantity=0, status="outOfStock")
        body = self._get("/api/products/", inStock="true", page_size=100).json()

        skus = [item["sku"] for item in body["results"]]
        self.assertNotIn("SKU-000", skus)
        self.assertEqual(body["pagination"]["count"], 24)

    def test_in_stock_filter_keeps_on_order_products(self):
        """«Под заказ» заказать можно, хотя остаток нулевой."""
        Product.objects.filter(sku="SKU-000").update(quantity=0, status="onOrder")
        body = self._get("/api/products/", inStock="true", page_size=100).json()

        self.assertIn("SKU-000", [item["sku"] for item in body["results"]])

    def test_brand_filter(self):
        Product.objects.filter(sku="SKU-001").update(brand="Novol")
        body = self._get("/api/products/", brand="Novol").json()

        self.assertEqual(body["pagination"]["count"], 1)
        self.assertEqual(body["results"][0]["sku"], "SKU-001")

    def test_filters_combine_and_apply_before_pagination(self):
        Product.objects.filter(sku__in=["SKU-001", "SKU-002"]).update(brand="Novol")
        Product.objects.filter(sku="SKU-002").update(quantity=0, status="outOfStock")
        body = self._get("/api/products/", brand="Novol", inStock="true").json()

        self.assertEqual(body["pagination"]["count"], 1)
        self.assertEqual(body["results"][0]["sku"], "SKU-001")

    def test_cyrillic_search_is_case_insensitive(self):
        Product.objects.filter(sku="SKU-003").update(name="Грунт акриловый")
        body = self._get("/api/products/", search="грунт").json()

        self.assertEqual(body["pagination"]["count"], 1)
        self.assertEqual(body["results"][0]["sku"], "SKU-003")
