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
            result = search.search_products(self._all(), "л")
        self.assertEqual([p.sku for p in result], ["LAC-200", "PRM-100"])

    @override_settings(ELASTICSEARCH_URL="http://es:9200")
    def test_elasticsearch_scopes_to_given_queryset(self):
        """ES не должен протаскивать товары, отфильтрованные до поиска."""
        hits = {"hits": {"hits": [{"_id": str(self.lacquer.id)}, {"_id": str(self.primer.id)}]}}
        base = Product.objects.filter(brand="Novol")
        with patch.object(search, "_request", return_value=hits):
            result = search.search_products(base, "л")
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


class ProductSynonymSearchTests(TestCase):
    def setUp(self):
        self.dist = Distributor.objects.create(name="Первый", inn="111")
        self.other = Distributor.objects.create(name="Второй", inn="222")
        self.product = Product.objects.create(distributor=self.dist, sku="FILM-1",
            name="Защитная пленка", brand="СмЕшАнНЫй БРЁНД", category="Укрывные материалы",
            synonyms=" укрывной материал; пленка; защитная плёнка; ПЛЁНКА; ; ",
            price=450, quantity=12, images=["https://example.com/a.jpg"])
        self.foreign = Product.objects.create(distributor=self.other, sku="FILM-1",
            name="Чужой товар", synonyms=["укрывной материал"], category="Другая")

    def test_full_partial_case_and_yo_matches_same_product(self):
        for query in ("укрывной материал", "УКРЫВНОЙ", "пленка", "ПЛЁНКА",
                      "защитная плёнка", "ЗАЩИТНАЯ ПЛЕНКА", "териал", "film-1",
                      "смешанный бренД", "укрывные"):
            with self.subTest(query=query):
                qs = Product.objects.filter(distributor=self.dist)
                self.assertEqual(list(search.search_products(qs, query)), [self.product])
        self.assertEqual(self.product.synonyms, ["укрывной материал", "пленка", "защитная плёнка"])
        self.assertEqual(Product.objects.count(), 2)

    def test_synonyms_replace_and_partial_save_keeps_other_persisted_fields(self):
        self.product.name = "Не сохранено"
        self.product.synonyms = ["маскировочная пленка"]
        self.product.save(update_fields=["synonyms"])
        qs = Product.objects.filter(distributor=self.dist)
        self.assertEqual(search.search_products(qs, "маскировочная").count(), 1)
        self.assertEqual(search.search_products(qs, "укрывной материал").count(), 0)
        self.assertEqual(search.search_products(qs, "защитная").count(), 1)
        self.product.synonyms = []
        self.product.save(update_fields=["synonyms"])
        self.assertEqual(search.search_products(qs, "маскировочная").count(), 0)

    def test_bulk_paths_rebuild_search_data(self):
        product = Product(distributor=self.dist, sku="BULK", name="ЛёГкИй", category="Другое", synonyms=["Прозрачный"])
        Product.objects.bulk_create([product])
        qs = Product.objects.filter(sku="BULK")
        self.assertEqual(search.search_products(qs, "легкий").count(), 1)
        qs.update(name="ТоНкИй")
        self.assertEqual(search.search_products(qs, "тонкий").count(), 1)
        product.synonyms = ["Новый вариант"]
        Product.objects.bulk_update([product], ["synonyms"])
        self.assertEqual(search.search_products(qs, "новый вариант").count(), 1)
        self.assertEqual(search.search_products(qs, "прозрачный").count(), 0)

    def test_bulk_conflict_preserves_untouched_search_fields(self):
        Product.objects.bulk_create([
            Product(distributor=self.dist, sku="FILM-1", name="Другое",
                category="Другое", synonyms=["маскировочная пленка"])
        ], update_conflicts=True, unique_fields=["distributor", "sku"], update_fields=["synonyms"])
        qs = Product.objects.filter(distributor=self.dist)
        self.assertEqual(search.search_products(qs, "защитная пленка").count(), 1)
        self.assertEqual(search.search_products(qs, "маскировочная").count(), 1)
        self.assertEqual(search.search_products(qs, "другое").count(), 0)

    @override_settings(ELASTICSEARCH_URL="http://es:9200")
    def test_stale_es_cannot_hide_or_add_matches(self):
        qs = Product.objects.filter(distributor=self.dist)
        for hits in ([], [{"_id": str(self.foreign.pk)}]):
            with patch.object(search, "_request", return_value={"hits": {"hits": hits}}):
                self.assertEqual(list(search.search_products(qs, "укрывной материал")), [self.product])

    def test_admin_form_accepts_semicolon_and_uses_shared_search(self):
        from .admin import ProductAdminForm, ProductAdmin
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory
        form = ProductAdminForm(instance=self.product)
        self.assertEqual(form.initial["synonyms"], "; ".join(self.product.synonyms))
        admin = ProductAdmin(Product, AdminSite())
        result, duplicates = admin.get_search_results(RequestFactory().get("/"),
            Product.objects.filter(distributor=self.dist), "ПЛЁНКА")
        self.assertEqual(list(result), [self.product])
        self.assertFalse(duplicates)
        import json
        from django.forms.models import model_to_dict
        data = model_to_dict(self.product)
        data["images"] = json.dumps(data["images"])
        data["synonyms"] = " маскировочная пленка; МАСКИРОВОЧНАЯ ПЛЁНКА; "
        form = ProductAdminForm(data=data, instance=self.product)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.product.refresh_from_db()
        self.assertEqual(self.product.synonyms, ["маскировочная пленка"])
        self.assertEqual(search.search_products(Product.objects.filter(pk=self.product.pk), "маскировочная").count(), 1)


@override_settings(ELASTICSEARCH_URL="", SECURE_SSL_REDIRECT=False)
class PreciseProductSearchTests(TestCase):
    def setUp(self):
        self.dist = Distributor.objects.create(name="Search", inn="search")
        self.other = Distributor.objects.create(name="Other", inn="other-search")

    def product(self, sku, name, **kwargs):
        return Product.objects.create(distributor=self.dist, sku=sku, name=name, **kwargs)

    def results(self, query):
        return list(search.search_products(Product.objects.filter(distributor=self.dist), query))

    def test_article_punctuation_spacing_and_compact_name(self):
        article = self.product("13-10", "Шлифблок (мягкий)")
        paper = self.product("P 800", "Наждачная бумага")
        for query, expected in (("13-10", article), ("1310", article), ("13 10", article),
                                ("p800", paper), ("мягкий шлифблок", article),
                                ("шлифблок (мягкий)", article), ("нажд бум", paper),
                                ("наждачнаябумага", paper)):
            with self.subTest(query=query):
                self.assertEqual(self.results(query), [expected])
        self.assertEqual(self.results("мягкий отсутствует"), [])
        # AND may use different fields, in either order.
        self.assertEqual(self.results("мягкий 1310"), [article])
        self.assertEqual(self.results("1310 мягкий"), [article])

    def test_lookalikes_case_yo_and_unicode_punctuation(self):
        paper = self.product("3M P120", "Лист 230х280 лёгкий")
        for query in ("230x280", "230Х280", "3М Р120", "3m p120", "ЛЁГКИЙ", "3М—Р120"):
            with self.subTest(query=query):
                self.assertEqual(self.results(query), [paper])
        from .services.product_names import normalize_search_text
        self.assertEqual(normalize_search_text("АВЕКМНОРСТУХ ё /!?_ 🧽"), "abekmhopctyx e")
        self.assertEqual(self.results(" /!?_ "), [paper])

    def ranked_products(self):
        exact = self.product("13-10", "Z exact", category="Z")
        prefix = self.product("13-100", "Z prefix", category="Z")
        name = self.product("OTHER", "13 10 name", category="Z")
        rest = self.product("REST", "A rest", category="A", synonyms=["10 13"])
        return [exact, prefix, name, rest]

    def test_article_name_groups_and_remaining_category_name_order(self):
        expected = self.ranked_products()
        rest2 = self.product("REST2", "B rest", category="A", synonyms=["13 10"])
        rest3 = self.product("REST3", "A rest", category="B", synonyms=["13 10"])
        self.assertEqual(self.results("13-10"), expected + [rest2, rest3])
        # Compact substring is SKU-only, not a match against brand/name lines.
        compact_sku = self.product("A13-10B", "Article substring")
        self.product("NO", "Unrelated", brand="13-10")
        self.assertIn(compact_sku, self.results("1 310"))
        self.assertNotIn(Product.objects.get(sku="NO"), self.results("1 310"))

    def test_es_rank_is_secondary_to_article_and_name_groups(self):
        expected = self.ranked_products()
        rest2 = self.product("REST2", "Z rest", category="Z", synonyms=["13 10"])
        with override_settings(ELASTICSEARCH_URL="http://es:9200"), patch.object(
            search, "_elasticsearch_ids", return_value=[rest2.pk] + [p.pk for p in reversed(expected)],
        ):
            self.assertEqual(self.results("13-10"), expected[:3] + [rest2, expected[3]])

    def test_client_catalog_preserves_ranking_and_distributor_scope(self):
        from django.contrib.auth.models import User
        from .models import AuthToken, ClientProfile, Region
        expected = self.ranked_products()
        foreign = Product.objects.create(distributor=self.other, sku="13-10", name="Foreign")
        inactive = self.product("13 10", "Inactive", is_active=False)
        user = User.objects.create_user(username="search-client")
        user.profile.role = "client"
        user.profile.save()
        region = Region.objects.create(code="SEARCH", name="Search")
        ClientProfile.objects.create(user=user, distributor=self.dist, region=region,
            status="active", company_name="Search client", inn="1234567890", city="Москва", contact_name="Test", phone="+79001112233")
        token = AuthToken.objects.create(user=user, key="search-client-token")
        for es in (False, True):
            with override_settings(ELASTICSEARCH_URL="http://es:9200" if es else ""), patch.object(
                search, "_elasticsearch_ids", return_value=[foreign.pk, inactive.pk] + [p.pk for p in reversed(expected)],
            ):
                response = self.client.get("/api/products/", {"search": "13-10"},
                    HTTP_AUTHORIZATION=f"Bearer {token.key}")
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual([row["id"] for row in body["results"]], [str(p.pk) for p in expected])
                self.assertEqual(body["count"], 4)
                self.assertEqual(set(body), {"results", "pagination", "count", "categories"})

    def test_batched_rebuild_covers_inactive_and_other_distributors(self):
        from io import StringIO
        from django.core.management import call_command, CommandError
        from .services.product_names import product_search_text
        products = [self.product("13-10", "Шлифблок"), self.product("P 800", "Лёгкий", is_active=False),
                    Product.objects.create(distributor=self.other, sku="3М Р120", name="Бумага", synonyms=["нажд бум"])]
        Product.objects.update(search_text="obsolete")
        call_command("rebuild_product_search", batch_size=1, stdout=StringIO())
        for product in products:
            product.refresh_from_db()
            self.assertEqual(product.search_text, product_search_text(product))
        self.assertEqual(self.results("1310"), [products[0]])
        with self.assertRaises(CommandError):
            call_command("rebuild_product_search", batch_size=0, stdout=StringIO())
