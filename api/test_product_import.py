import io

from django.test import TestCase, Client
from django.contrib.auth.models import User
from openpyxl import Workbook

from django.core.exceptions import ValidationError

from .models import (
    MAX_PRODUCT_IMAGES,
    AuthToken,
    Distributor,
    Product,
    Region,
    normalize_product_images,
)
from .services.product_import import parse_products_workbook, upsert_products


def _wb_like_template(rows):
    """Строит книгу в стиле WB: секции сверху, шапка на 3-й строке, данные с 5-й."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Товары"
    ws.append(["", "", "Основная информация"])            # строка 1 — секции
    ws.append([])                                          # строка 2 — пусто
    ws.append(["Группа", "Артикул продавца", "Артикул WB", "Наименование",
               "Категория продавца", "Бренд", "Описание", "Фото", "Баркод",
               "Вес с упаковкой (кг)", "ТНВЭД", "Ставка НДС"])  # строка 3 — шапка
    ws.append(["1", "Это подсказка", "", "", "", "", "", "", "", "", "", ""])  # строка 4 — помощь
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


class ProductImportParserTests(TestCase):
    def test_parses_wb_layout_and_skips_instruction_row(self):
        buf = _wb_like_template([
            ["1", "SKU-1", "1001", "Лак TC-2200", "Лаки", "HiQ", "Описание 1",
             "http://a/1.webp;http://a/2.webp", "2039157960415", "1.5", "", "5"],
            ["2", "SKU-2", "1002", "Грунт", "Грунты", "RANAL", "",
             "http://b/1.webp", "5906007001826", "0.3", "3403199000", "5"],
        ])
        products, errors = parse_products_workbook(buf)
        self.assertEqual(errors, [])
        self.assertEqual(len(products), 2)  # строка-подсказка пропущена
        p = products[0]
        self.assertEqual(p["sku"], "SKU-1")
        self.assertEqual(p["wb_article"], "1001")
        self.assertEqual(p["images"], ["http://a/1.webp", "http://a/2.webp"])
        self.assertEqual(p["barcode"], "2039157960415")
        self.assertEqual(str(p["weight"]), "1.5")
        self.assertFalse(p["_has_price"])
        self.assertFalse(p["_has_quantity"])

    def test_keeps_first_15_images_and_warns(self):
        links = ";".join(f"http://a/{i}.webp" for i in range(1, 21))  # 20 ссылок
        buf = _wb_like_template([
            ["1", "SKU-1", "1001", "Лак", "Лаки", "HiQ", "", links, "", "1.5", "", "5"],
        ])
        products, errors = parse_products_workbook(buf)
        self.assertEqual(len(products[0]["images"]), MAX_PRODUCT_IMAGES)
        self.assertEqual(products[0]["images"][0], "http://a/1.webp")
        self.assertEqual(products[0]["images"][-1], f"http://a/{MAX_PRODUCT_IMAGES}.webp")
        self.assertTrue(any("сохранены первые 15" in e for e in errors), errors)

    def test_drops_duplicates_and_non_links(self):
        cell = "http://a/1.webp;http://a/1.webp; ;нет фото;https://b/2.webp"
        buf = _wb_like_template([
            ["1", "SKU-1", "1001", "Лак", "Лаки", "HiQ", "", cell, "", "1.5", "", "5"],
        ])
        products, errors = parse_products_workbook(buf)
        self.assertEqual(products[0]["images"], ["http://a/1.webp", "https://b/2.webp"])
        self.assertTrue(any("без http(s)-ссылки" in e for e in errors), errors)

    def test_rejects_file_without_required_columns(self):
        wb = Workbook()
        ws = wb.active
        ws.append(["foo", "bar"])
        ws.append([1, 2])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        products, errors = parse_products_workbook(buf)
        self.assertEqual(products, [])
        self.assertTrue(errors)


class StockUploadFileEndpointTests(TestCase):
    def setUp(self):
        self.http = Client()
        self.user = User.objects.create_user(username="dist", password="pw")
        self.user.profile.role = "distributor"
        self.user.profile.save()
        self.distributor = Distributor.objects.create(
            user=self.user, name="Dist", inn="1112223334", phone="1", email="d@e.co"
        )
        Region.objects.create(code="77", name="Msk", distributor=self.distributor)
        self.token = AuthToken.objects.create(key="dist-token", user=self.user)

    def _upload(self, buf):
        return self.http.post(
            "/api/distributor/stock/upload-file/",
            data={"file": buf},
            HTTP_AUTHORIZATION=f"Bearer {self.token.key}",
        )

    def test_upload_creates_products_and_preserves_stock_on_reimport(self):
        rows = [
            ["1", "SKU-1", "1001", "Лак", "Лаки", "HiQ", "d", "http://a/1.webp", "2039157960415", "1.5", "", "5"],
        ]
        buf = _wb_like_template(rows)
        buf.name = "wb.xlsx"
        resp = self._upload(buf)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["created"], 1)

        # Дистрибьютор проставил цену и остаток вручную
        p = Product.objects.get(distributor=self.distributor, sku="SKU-1")
        self.assertEqual(p.barcode, "2039157960415")
        self.assertEqual(p.images, ["http://a/1.webp"])
        p.price = 4200
        p.quantity = 9
        p.save()

        # Повторная загрузка того же файла (без цен/остатков) не должна их обнулить
        buf2 = _wb_like_template(rows)
        buf2.name = "wb.xlsx"
        resp2 = self._upload(buf2)
        self.assertEqual(resp2.json()["updated"], 1)
        p.refresh_from_db()
        self.assertEqual(int(p.price), 4200)
        self.assertEqual(p.quantity, 9)

    def test_upload_stores_up_to_15_images(self):
        links = ";".join(f"http://a/{i}.webp" for i in range(1, 19))
        buf = _wb_like_template([
            ["1", "SKU-IMG", "1001", "Лак", "Лаки", "HiQ", "d", links, "2039157960415", "1.5", "", "5"],
        ])
        buf.name = "wb.xlsx"
        resp = self._upload(buf)
        self.assertEqual(resp.status_code, 200, resp.content)
        p = Product.objects.get(distributor=self.distributor, sku="SKU-IMG")
        self.assertEqual(len(p.images), MAX_PRODUCT_IMAGES)

    def test_upload_requires_file(self):
        resp = self.http.post(
            "/api/distributor/stock/upload-file/",
            HTTP_AUTHORIZATION=f"Bearer {self.token.key}",
        )
        self.assertEqual(resp.status_code, 400)


class ProductImagesModelTests(TestCase):
    """Лимит в 15 фото держится на уровне модели, а не только импорта."""

    def setUp(self):
        user = User.objects.create_user(username="dist2", password="pw")
        self.distributor = Distributor.objects.create(
            user=user, name="Dist2", inn="1112223335", phone="1", email="d2@e.co"
        )

    def _product(self, images):
        return Product.objects.create(
            distributor=self.distributor, sku="SKU-M", name="Товар",
            category="Лаки", images=images,
        )

    def test_save_truncates_to_limit(self):
        p = self._product([f"http://a/{i}.jpg" for i in range(20)])
        p.refresh_from_db()
        self.assertEqual(len(p.images), MAX_PRODUCT_IMAGES)

    def test_save_accepts_semicolon_string(self):
        p = self._product("http://a/1.jpg;http://a/2.jpg")
        p.refresh_from_db()
        self.assertEqual(p.images, ["http://a/1.jpg", "http://a/2.jpg"])

    def test_full_clean_rejects_more_than_limit(self):
        p = Product(
            distributor=self.distributor, sku="SKU-V", name="Товар", category="Лаки",
            images=[f"http://a/{i}.jpg" for i in range(MAX_PRODUCT_IMAGES + 1)],
        )
        with self.assertRaises(ValidationError):
            p.full_clean()

    def test_normalize_helper(self):
        self.assertEqual(normalize_product_images(None), [])
        self.assertEqual(normalize_product_images(["", "  ", "ftp://x/1.jpg"]), [])
        self.assertEqual(
            normalize_product_images(["http://a/1.jpg", "http://a/1.jpg"]),
            ["http://a/1.jpg"],
        )


class ProductSynonymImportTests(TestCase):
    setUp = StockUploadFileEndpointTests.setUp
    _upload = StockUploadFileEndpointTests._upload

    def workbook(self, synonyms=None, include=True):
        wb = Workbook()
        ws = wb.active
        ws.append(["Артикул", "Название"] + (["Синонимы"] if include else []))
        ws.append(["FILM", "Защитная пленка"] + ([synonyms] if include else []))
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        buf.name = "price.xlsx"
        return buf

    def test_import_replaces_preserves_and_clears_without_duplicates(self):
        from .services.search import search_products
        from .models import ClientPriceOverride, ClientProfile
        from decimal import Decimal
        product = Product.objects.create(distributor=self.distributor, sku="FILM", name="Пленка",
            category="Материалы", price=999, quantity=7, images=["https://example.com/a.jpg"])
        other = Distributor.objects.create(name="Other", inn="555")
        foreign = Product.objects.create(distributor=other, sku="FILM", name="Чужой", category="Материалы", synonyms=["чужой"])
        client = ClientProfile.objects.create(user=User.objects.create_user(username="buyer"),
            distributor=self.distributor, region=Region.objects.filter(distributor=self.distributor).first(), inn="1234567890", company_name="Buyer", city="Москва", contact_name="Buyer", phone="123")
        override = ClientPriceOverride.objects.create(client=client, product=product, price=500)
        for value, include, expected in (
            ("укрывной материал; пленка; защитная плёнка", True, ["укрывной материал", "пленка", "защитная плёнка"]),
            (None, False, ["укрывной материал", "пленка", "защитная плёнка"]),
            ("маскировочная пленка", True, ["маскировочная пленка"]),
            (None, True, []),
        ):
            response = self._upload(self.workbook(value, include))
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(response.json()["updated"], 1)
            product.refresh_from_db()
            self.assertEqual(product.synonyms, expected)
            self.assertEqual(product.price, Decimal("999"))
            self.assertEqual(product.quantity, 7)
            self.assertEqual(product.images, ["https://example.com/a.jpg"])
            self.assertEqual(ClientPriceOverride.objects.get(pk=override.pk).price, Decimal("500"))
        foreign.refresh_from_db()
        self.assertEqual(foreign.synonyms, ["чужой"])
        self.assertEqual(Product.objects.count(), 2)
        self.assertEqual(search_products(Product.objects.filter(distributor=self.distributor), "маскировочная").count(), 0)

    def test_json_upload_and_catalog_share_synonyms(self):
        auth = {"HTTP_AUTHORIZATION": f"Bearer {self.token.key}"}
        response = self.http.post("/api/distributor/stock/upload/", {"items": [
            {"sku": "FILM", "name": "Защитная пленка", "synonyms": "укрывной материал; пленка"}
        ]}, content_type="application/json", **auth)
        self.assertEqual(response.status_code, 200, response.content)
        response = self.http.get("/api/distributor/stock/", {"search": "УКРЫВНОЙ"}, **auth)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["results"][0]["synonyms"], ["укрывной материал", "пленка"])

    def test_api_rejects_malformed_synonyms(self):
        response = self.http.post("/api/distributor/stock/upload/", {"items": [
            {"sku": "BAD", "synonyms": {"bad": "value"}}
        ]}, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.token.key}")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Product.objects.filter(sku="BAD").exists())
