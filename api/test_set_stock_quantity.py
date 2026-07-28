from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from .models import Distributor, Product


class SetStockQuantityTests(TestCase):
    def setUp(self):
        self.distributor = Distributor.objects.create(
            name="Dist", inn="1112223334", phone="1", email="d@e.co"
        )
        self.other = Distributor.objects.create(
            name="Dist2", inn="1112223335", phone="2", email="d2@e.co"
        )

    def _product(self, sku, quantity, status="inStock", distributor=None, is_active=True, price="500"):
        return Product.objects.create(
            distributor=distributor or self.distributor,
            sku=sku, name=f"Товар {sku}", category="Лаки",
            price=Decimal(price), quantity=quantity, status=status, is_active=is_active,
        )

    def _run(self, *args):
        out = StringIO()
        call_command("set_stock_quantity", *args, stdout=out)
        return out.getvalue()

    def test_sets_quantity_for_every_product(self):
        empty = self._product("EMPTY", 0, status="outOfStock")
        few = self._product("FEW", 3, status="low")
        many = self._product("MANY", 500, status="inStock")

        self._run("--apply")

        for product in (empty, few, many):
            product.refresh_from_db()
            self.assertEqual(product.quantity, 10, product.sku)

    def test_status_follows_new_quantity(self):
        out_of_stock = self._product("OOS", 0, status="outOfStock")
        low = self._product("LOW", 2, status="low")
        self._run("--apply")

        out_of_stock.refresh_from_db()
        low.refresh_from_db()
        # «Нет в наличии» блокирует заказ на клиенте — снимаем.
        self.assertEqual(out_of_stock.status, "inStock")
        # Остальные статусы оставляем как есть, это решение дистрибьютора.
        self.assertEqual(low.status, "low")

    def test_price_is_never_touched(self):
        product = self._product("PRICE", 0, price="1234.56")
        self._run("--apply")
        product.refresh_from_db()
        self.assertEqual(product.price, Decimal("1234.56"))

    def test_photos_survive(self):
        product = self._product("PHOTO", 0)
        product.images = ["https://cdn.example.com/1.webp"]
        product.save()
        self._run("--apply")
        product.refresh_from_db()
        self.assertEqual(product.images, ["https://cdn.example.com/1.webp"])

    def test_custom_quantity(self):
        product = self._product("CUSTOM", 0, status="outOfStock")
        self._run("--apply", "--quantity", "3")
        product.refresh_from_db()
        self.assertEqual(product.quantity, 3)
        self.assertEqual(product.status, "low")  # 3 шт. — «мало»

    def test_keep_status_flag(self):
        product = self._product("OOS", 0, status="outOfStock")
        self._run("--apply", "--keep-status")
        product.refresh_from_db()
        self.assertEqual(product.quantity, 10)
        self.assertEqual(product.status, "outOfStock")

    def test_skip_on_order(self):
        on_order = self._product("ONORDER", 0, status="onOrder")
        normal = self._product("NORMAL", 0, status="outOfStock")
        self._run("--apply", "--skip-on-order")

        on_order.refresh_from_db()
        normal.refresh_from_db()
        self.assertEqual(on_order.quantity, 0)
        self.assertEqual(normal.quantity, 10)

    def test_on_order_updated_by_default(self):
        on_order = self._product("ONORDER", 0, status="onOrder")
        self._run("--apply")
        on_order.refresh_from_db()
        self.assertEqual(on_order.quantity, 10)
        self.assertEqual(on_order.status, "onOrder")  # статус «под заказ» сохраняем

    def test_dry_run_writes_nothing(self):
        product = self._product("DRY", 0)
        output = self._run()
        product.refresh_from_db()
        self.assertEqual(product.quantity, 0)
        self.assertIn("Пробный прогон", output)

    def test_distributor_scope(self):
        mine = self._product("MINE", 0)
        theirs = self._product("THEIRS", 0, distributor=self.other)
        self._run("--apply", "--distributor", str(self.distributor.id))
        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertEqual(mine.quantity, 10)
        self.assertEqual(theirs.quantity, 0)

    def test_only_active_flag(self):
        hidden = self._product("HIDDEN", 0, is_active=False)
        self._run("--apply", "--only-active")
        hidden.refresh_from_db()
        self.assertEqual(hidden.quantity, 0)

    def test_untouched_products_keep_updated_at(self):
        product = self._product("SAME", 10, status="inStock")
        before = product.updated_at
        self._run("--apply")
        product.refresh_from_db()
        self.assertEqual(product.updated_at, before)

    def test_negative_quantity_fails(self):
        with self.assertRaises(CommandError):
            self._run("--apply", "--quantity", "-1")

    def test_unknown_distributor_fails(self):
        with self.assertRaises(CommandError):
            self._run("--apply", "--distributor", "999999")
