"""Проставляет одинаковый остаток ВСЕМ товарам.

В отличие от backfill_price_quantity, который заполняет только пустые
значения, эта команда перезаписывает остаток у каждого товара в выборке —
включая те, где остаток уже проставлен. Цену не трогает вообще.

Использование:
    python manage.py set_stock_quantity                    # что изменится (без записи)
    python manage.py set_stock_quantity --apply            # всем остаток 10 шт.
    python manage.py set_stock_quantity --apply --quantity 25
    python manage.py set_stock_quantity --apply --distributor 8

Вместе с остатком выравнивается статус: «нет в наличии» → «в наличии»
(или «мало» при остатке ≤ 5), иначе клиент не сможет добавить товар в заказ.
Отключается флагом --keep-status.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from api.models import Distributor, Product

DEFAULT_QUANTITY = 10
BATCH_SIZE = 500


class Command(BaseCommand):
    help = "Проставляет остаток 10 шт. всем товарам (перезаписывает существующий)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Записать изменения. Без этого флага команда только показывает, что изменится.",
        )
        parser.add_argument(
            "--quantity",
            type=int,
            default=DEFAULT_QUANTITY,
            help=f"Какой остаток проставить (по умолчанию {DEFAULT_QUANTITY}).",
        )
        parser.add_argument(
            "--distributor",
            type=int,
            help="Ограничить одним дистрибьютором (id). По умолчанию — все.",
        )
        parser.add_argument(
            "--only-active",
            action="store_true",
            help="Только товары, показываемые в приложении (is_active=True).",
        )
        parser.add_argument(
            "--skip-on-order",
            action="store_true",
            help=(
                "Не трогать товары «под заказ»: нулевой остаток у них — норма, "
                "заказать можно любое количество, а фиксированный остаток это ограничит."
            ),
        )
        parser.add_argument(
            "--keep-status",
            action="store_true",
            help="Не трогать статус, менять только остаток.",
        )

    def handle(self, *args, **options):
        quantity = options["quantity"]
        if quantity < 0:
            raise CommandError("--quantity не может быть отрицательным.")

        qs = Product.objects.all()

        distributor_id = options.get("distributor")
        if distributor_id is not None:
            if not Distributor.objects.filter(pk=distributor_id).exists():
                raise CommandError(f"Дистрибьютор id={distributor_id} не найден.")
            qs = qs.filter(distributor_id=distributor_id)

        if options["only_active"]:
            qs = qs.filter(is_active=True)

        skip_on_order = options["skip_on_order"]
        if skip_on_order:
            qs = qs.exclude(status="onOrder")

        keep_status = options["keep_status"]
        targets = qs.order_by("id")
        total = targets.count()
        already = targets.filter(quantity=quantity).count()

        self.stdout.write(f"Товаров в выборке: {total}")
        self.stdout.write(f"  уже с остатком {quantity}: {already}")
        self.stdout.write(f"  будет изменено:  {total - already}")
        if skip_on_order:
            self.stdout.write("  товары «под заказ» исключены (--skip-on-order)")

        if not total:
            self.stdout.write(self.style.SUCCESS("Нечего обновлять."))
            return

        if not options["apply"]:
            self.stdout.write("")
            self.stdout.write("Примеры (первые 10):")
            for product in targets[:10]:
                self.stdout.write(f"  {self._preview(product, quantity, keep_status)}")
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING("Пробный прогон: ничего не записано. Повторите с флагом --apply.")
            )
            return

        updated = self._apply(targets, quantity, keep_status)
        self.stdout.write(self.style.SUCCESS(f"Обновлено товаров: {updated}"))

    def _fill(self, product, quantity, keep_status):
        """Проставляет остаток и (при необходимости) статус. Возвращает изменённые поля."""
        changed = []
        if product.quantity != quantity:
            product.quantity = quantity
            changed.append("quantity")
        # Остаток без статуса бесполезен: при status="outOfStock" клиент не
        # сможет добавить товар в заказ, даже если на складе что-то есть.
        if not keep_status and product.status == "outOfStock" and quantity > 0:
            product.status = "inStock" if quantity > 5 else "low"
            changed.append("status")
        return changed

    def _preview(self, product, quantity, keep_status):
        before = f"остаток {product.quantity}, статус {product.status}"
        changed = self._fill(product, quantity, keep_status)
        if not changed:
            return f"{product.sku} · {product.name[:40]}: {before} → без изменений"
        after = f"остаток {product.quantity}, статус {product.status}"
        return f"{product.sku} · {product.name[:40]}: {before} → {after}"

    def _apply(self, targets, quantity, keep_status):
        """Обновляет товары пачками.

        bulk_update вместо save(): фото и остальные поля остаются нетронутыми,
        сигналы не дёргаются. updated_at проставляем вручную — auto_now
        работает только через save().
        """
        now = timezone.now()
        fields = ["quantity", "status", "updated_at"]
        updated = 0
        batch = []

        for product in targets.iterator(chunk_size=BATCH_SIZE):
            if not self._fill(product, quantity, keep_status):
                continue
            product.updated_at = now
            batch.append(product)
            if len(batch) >= BATCH_SIZE:
                updated += self._flush(batch, fields)
                batch = []

        updated += self._flush(batch, fields)
        return updated

    def _flush(self, batch, fields):
        if not batch:
            return 0
        with transaction.atomic():
            Product.objects.bulk_update(batch, fields, batch_size=BATCH_SIZE)
        return len(batch)
