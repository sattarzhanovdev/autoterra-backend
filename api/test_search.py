"""Тесты слоя поиска по товарам."""

from unittest.mock import patch

from django.test import TestCase, override_settings

from .models import Distributor, Product
from .services import search


class ProductSearchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.distributor = Distributor.objects.create(
            name="Дистрибьютор", inn="7701000001"
        )
        cls.primer = Product.objects.create(
            distributor=cls.distributor, sku="PRM-100", name="Грунт акриловый",
            category="Грунтовки", brand="AutoTerra", price=100, quantity=5,
        )
        cls.lacquer = Product.objects.create(
            distributor=cls.distributor, sku="LAC-200", name="Лак глянцевый",
            category="Лаки", brand="Novol", price=200, quantity=5,
        )

    def _all(self):
        return Product.objects.all()

    # ── поиск в БД (текущее развёртывание) ──────────────────────────────────
    def test_empty_query_returns_queryset_unchanged(self):
        result = search.search_products(self._all(), "  ")
        self.assertEqual(result.count(), 2)

    def test_database_search_matches_name(self):
        result = search.search_products(self._all(), "грунт")
        self.assertEqual([p.sku for p in result], ["PRM-100"])

    def test_database_search_matches_sku_and_brand(self):
        self.assertEqual(
            [p.sku for p in search.search_products(self._all(), "LAC-200")], ["LAC-200"]
        )
        self.assertEqual(
            [p.sku for p in search.search_products(self._all(), "novol")], ["LAC-200"]
        )

    def test_search_returns_queryset_for_further_filtering(self):
        """Выдача должна оставаться QuerySet: дальше идут фильтры и пагинация."""
        result = search.search_products(self._all(), "лак")
        self.assertEqual(result.filter(brand="Novol").count(), 1)
        self.assertEqual(result.filter(brand="AutoTerra").count(), 0)

    def test_elasticsearch_disabled_by_default(self):
        self.assertFalse(search.is_elasticsearch_enabled())

    # ── поиск через Elasticsearch ───────────────────────────────────────────
    @override_settings(ELASTICSEARCH_URL="http://es:9200")
    def test_elasticsearch_results_keep_relevance_order(self):
        hits = {"hits": {"hits": [{"_id": str(self.lacquer.id)}, {"_id": str(self.primer.id)}]}}
        with patch.object(search, "_request", return_value=hits):
            result = search.search_products(self._all(), "покрытие")
        self.assertEqual([p.sku for p in result], ["LAC-200", "PRM-100"])

    @override_settings(ELASTICSEARCH_URL="http://es:9200")
    def test_elasticsearch_scopes_to_given_queryset(self):
        """ES не должен протаскивать товары, отфильтрованные до поиска."""
        hits = {"hits": {"hits": [{"_id": str(self.lacquer.id)}, {"_id": str(self.primer.id)}]}}
        base = Product.objects.filter(brand="Novol")
        with patch.object(search, "_request", return_value=hits):
            result = search.search_products(base, "покрытие")
        self.assertEqual([p.sku for p in result], ["LAC-200"])

    @override_settings(ELASTICSEARCH_URL="http://es:9200")
    def test_falls_back_to_database_when_elasticsearch_fails(self):
        with patch.object(search, "_request", side_effect=OSError("connection refused")):
            result = search.search_products(self._all(), "грунт")
        self.assertEqual([p.sku for p in result], ["PRM-100"])

    @override_settings(ELASTICSEARCH_URL="http://es:9200")
    def test_no_matches_returns_empty_not_everything(self):
        with patch.object(search, "_request", return_value={"hits": {"hits": []}}):
            result = search.search_products(self._all(), "несуществующее")
        self.assertEqual(result.count(), 0)

    @override_settings(ELASTICSEARCH_URL="http://es:9200")
    def test_malformed_response_falls_back_to_database(self):
        with patch.object(search, "_request", return_value={"unexpected": True}):
            result = search.search_products(self._all(), "лак")
        self.assertEqual([p.sku for p in result], ["LAC-200"])

    def test_indexing_is_noop_without_elasticsearch(self):
        self.assertFalse(search.index_product(self.primer))
        self.assertEqual(search.bulk_index([self.primer]), 0)
        self.assertFalse(search.ensure_index())

    # ── регистр в кириллице ────────────────────────────────────────────────
    def test_cyrillic_search_ignores_case(self):
        """На SQLite LIKE регистронезависим только для латиницы."""
        for query in ("грунт", "Грунт", "ГРУНТ"):
            with self.subTest(query=query):
                result = search.search_products(self._all(), query)
                self.assertEqual(
                    [p.sku for p in result], ["PRM-100"], f"запрос «{query}»"
                )

    def test_latin_search_ignores_case(self):
        for query in ("novol", "NOVOL", "Novol"):
            with self.subTest(query=query):
                result = search.search_products(self._all(), query)
                self.assertEqual([p.sku for p in result], ["LAC-200"])
