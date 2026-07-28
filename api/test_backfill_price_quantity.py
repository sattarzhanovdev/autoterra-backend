from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from .models import Distributor, Product


class BackfillPriceQuantityTests(TestCase):
    def setUp(self):
        self.distributor = Distributor.objects.create(
            name="Dist", inn="1112223334", phone="1", email="d@e.co"
        )
        self.other = Distributor.objects.create(
            name="Dist2", inn="1112223335", phone="2", email="d2@e.co"
        )

    def _product(self, sku, price, quantity, status="outOfStock", distributor=None, is_active=True):
        return Product.objects.create(
            distributor=distributor or self.distributor,
            sku=sku, name=f"Товар {sku}", category="Лаки",
            price=price, quantity=quantity, status=status, is_active=is_active,
        )

    def _run(self, *args):
        out = StringIO()
        call_command("backfill_price_quantity", *args, stdout=out)
        return out.getvalue()

    def test_fills_missing_price_and_quantity(self):
        both = self._product("BOTH", 0, 0)
        self._run("--apply")
        both.refresh_from_db()
        self.assertEqual(both.price, Decimal("10"))
        self.assertEqual(both.quantity, 10)

    def test_fills_only_the_missing_column(self):
        no_price = self._product("NOPRICE", 0, 7, status="inStock")
        no_qty = self._product("NOQTY", Decimal("540"), 0)
        self._run("--apply")

        no_price.refresh_from_db()
        self.assertEqual(no_price.price, Decimal("10"))
        self.assertEqual(no_price.quantity, 7)  # существующий остаток не тронут

        no_qty.refresh_from_db()
        self.assertEqual(no_qty.price, Decimal("540"))  # цена не перезаписана
        self.assertEqual(no_qty.quantity, 10)

    def test_does_not_touch_filled_products(self):
        ok = self._product("OK", Decimal("500"), 3, status="low")
        before = ok.updated_at
        self._run("--apply")
        ok.refresh_from_db()
        self.assertEqual(ok.price, Decimal("500"))
        self.assertEqual(ok.quantity, 3)
        self.assertEqual(ok.updated_at, before)

    def test_status_follows_new_quantity(self):
        out_of_stock = self._product("OOS", Decimal("100"), 0, status="outOfStock")
        self._run("--apply")
        out_of_stock.refresh_from_db()
        # Иначе клиент не смог бы добавить товар в заказ: _canOrder блокирует outOfStock.
        self.assertEqual(out_of_stock.status, "inStock")

    def test_keep_status_flag(self):
        out_of_stock = self._product("OOS", Decimal("100"), 0, status="outOfStock")
        self._run("--apply", "--keep-status")
        out_of_stock.refresh_from_db()
        self.assertEqual(out_of_stock.quantity, 10)
        self.assertEqual(out_of_stock.status, "outOfStock")

    def test_skip_on_order_keeps_unlimited_stock(self):
        on_order = self._product("ONORDER", 0, 0, status="onOrder")
        self._run("--apply", "--skip-on-order")
        on_order.refresh_from_db()
        self.assertEqual(on_order.price, Decimal("10"))  # цену всё равно проставляем
        self.assertEqual(on_order.quantity, 0)  # остаток «под заказ» не ограничиваем

    def test_on_order_filled_by_default(self):
        on_order = self._product("ONORDER", 0, 0, status="onOrder")
        self._run("--apply")
        on_order.refresh_from_db()
        self.assertEqual(on_order.quantity, 10)

    def test_dry_run_writes_nothing(self):
        product = self._product("DRY", 0, 0)
        output = self._run()
        product.refresh_from_db()
        self.assertEqual(product.price, Decimal("0"))
        self.assertEqual(product.quantity, 0)
        self.assertIn("Пробный прогон", output)

    def test_custom_values(self):
        product = self._product("CUSTOM", 0, 0)
        self._run("--apply", "--price", "99.50", "--quantity", "5")
        product.refresh_from_db()
        self.assertEqual(product.price, Decimal("99.50"))
        self.assertEqual(product.quantity, 5)
        self.assertEqual(product.status, "low")  # 5 шт. — «мало», а не «в наличии»

    def test_distributor_scope(self):
        mine = self._product("MINE", 0, 0)
        theirs = self._product("THEIRS", 0, 0, distributor=self.other)
        self._run("--apply", "--distributor", str(self.distributor.id))
        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertEqual(mine.quantity, 10)
        self.assertEqual(theirs.quantity, 0)

    def test_only_active_flag(self):
        hidden = self._product("HIDDEN", 0, 0, is_active=False)
        self._run("--apply", "--only-active")
        hidden.refresh_from_db()
        self.assertEqual(hidden.quantity, 0)

    def test_unknown_distributor_fails(self):
        with self.assertRaises(CommandError):
            self._run("--apply", "--distributor", "999999")

    def test_negative_price_fails(self):
        with self.assertRaises(CommandError):
            self._run("--apply", "--price", "-5")

    def test_photos_survive_backfill(self):
        product = self._product("PHOTO", 0, 0)
        product.images = ["https://cdn.example.com/1.webp"]
        product.save()
        self._run("--apply")
        product.refresh_from_db()
        self.assertEqual(product.images, ["https://cdn.example.com/1.webp"])
        self.assertEqual(product.quantity, 10)
