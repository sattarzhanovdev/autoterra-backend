"""Проставляет цену и остаток товарам, у которых их нет.

После импорта из Excel-шаблона WB у товаров часто нет ни цены, ни остатка —
таких колонок в файле просто не бывает. Клиент видит такой товар как «нет в
наличии» и не может добавить его в заказ. Команда заполняет пустые значения
безопасными заглушками, не трогая уже проставленные вручную.

Использование:
    python manage.py backfill_price_quantity                  # что изменится (без записи)
    python manage.py backfill_price_quantity --apply          # записать изменения
    python manage.py backfill_price_quantity --apply --distributor 8
    python manage.py backfill_price_quantity --apply --price 10 --quantity 10

Пустым считается значение 0 или NULL. Заполняется только та колонка, которой
не хватает: если цена уже стоит, а остаток нулевой — обновится только остаток.
"""

from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from api.models import Distributor, Product

DEFAULT_PRICE = Decimal("10")
DEFAULT_QUANTITY = 10
BATCH_SIZE = 500


class Command(BaseCommand):
    help = "Проставляет цену 10 ₽ и остаток 10 шт. товарам, у которых их нет"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Записать изменения. Без этого флага команда только показывает, что изменится.",
        )
        parser.add_argument(
            "--price",
            default=str(DEFAULT_PRICE),
            help=f"Цена для товаров без цены (по умолчанию {DEFAULT_PRICE}).",
        )
        parser.add_argument(
            "--quantity",
            type=int,
            default=DEFAULT_QUANTITY,
            help=f"Остаток для товаров без остатка (по умолчанию {DEFAULT_QUANTITY}).",
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
                "Не трогать остаток у товаров «под заказ». У них нулевой остаток — "
                "это норма: заказать можно любое количество. Проставленные 10 шт. "
                "наоборот ограничат заказ десятью штуками. Цену такие товары получат."
            ),
        )
        parser.add_argument(
            "--keep-status",
            action="store_true",
            help=(
                "Не трогать статус. По умолчанию товару с проставленным остатком "
                "статус «нет в наличии» меняется на «в наличии» — иначе клиент "
                "всё равно не сможет добавить его в заказ."
            ),
        )

    def handle(self, *args, **options):
        price = self._price(options["price"])
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

        no_price = Q(price__isnull=True) | Q(price__lte=0)
        no_quantity = Q(quantity__isnull=True) | Q(quantity__lte=0)

        total = qs.count()
        targets = qs.filter(no_price | no_quantity).order_by("id")
        both_count = qs.filter(no_price & no_quantity).count()
        price_only = qs.filter(no_price & ~no_quantity).count()
        quantity_only = qs.filter(no_quantity & ~no_price).count()
        target_count = targets.count()

        self.stdout.write(f"Товаров в выборке: {total}")
        self.stdout.write(f"  без цены и без остатка: {both_count}")
        self.stdout.write(f"  только без цены:        {price_only}")
        self.stdout.write(f"  только без остатка:     {quantity_only}")
        # Кандидаты, а не итог: с --skip-on-order часть из них останется как есть.
        self.stdout.write(f"  итого кандидатов:       {target_count}")

        if not target_count:
            self.stdout.write(self.style.SUCCESS("Нечего обновлять."))
            return

        keep_status = options["keep_status"]
        skip_on_order = options["skip_on_order"]

        if not options["apply"]:
            self.stdout.write("")
            self.stdout.write("Примеры (первые 10):")
            for product in targets[:10]:
                self.stdout.write(f"  {self._preview(product, price, quantity, keep_status, skip_on_order)}")
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING("Пробный прогон: ничего не записано. Повторите с флагом --apply.")
            )
            return

        updated = self._apply(
            targets, price, quantity, keep_status=keep_status, skip_on_order=skip_on_order,
        )
        self.stdout.write(self.style.SUCCESS(f"Обновлено товаров: {updated}"))

    def _price(self, raw):
        try:
            value = Decimal(str(raw).replace(",", "."))
        except (InvalidOperation, ValueError):
            raise CommandError(f"Некорректная цена: {raw}")
        if value < 0:
            raise CommandError("--price не может быть отрицательной.")
        return value

    def _fill(self, product, price, quantity, keep_status, skip_on_order=False):
        """Заполняет пустые поля товара. Возвращает список изменённых полей."""
        changed = []
        if product.price is None or product.price <= 0:
            product.price = price
            changed.append("price")
        fill_quantity = not (skip_on_order and product.status == "onOrder")
        if fill_quantity and (product.quantity is None or product.quantity <= 0):
            product.quantity = quantity
            changed.append("quantity")
        # Остаток без статуса бесполезен: при status="outOfStock" клиент не
        # сможет добавить товар в заказ, даже если на складе что-то есть.
        if (
            "quantity" in changed
            and not keep_status
            and product.status == "outOfStock"
            and product.quantity > 0
        ):
            product.status = "inStock" if product.quantity > 5 else "low"
            changed.append("status")
        return changed

    def _preview(self, product, price, quantity, keep_status, skip_on_order):
        before = f"цена {product.price}, остаток {product.quantity}, статус {product.status}"
        changed = self._fill(product, price, quantity, keep_status, skip_on_order)
        after = f"цена {product.price}, остаток {product.quantity}, статус {product.status}"
        if not changed:
            return f"{product.sku} · {product.name[:40]}: {before} → без изменений"
        return f"{product.sku} · {product.name[:40]}: {before} → {after} ({', '.join(changed)})"

    def _apply(self, targets, price, quantity, keep_status, skip_on_order):
        """Обновляет товары пачками.

        bulk_update вместо save(): не тянет за собой сигналы и лишние записи,
        а фото и прочие поля остаются нетронутыми. updated_at проставляем
        вручную — auto_now работает только через save().
        """
        now = timezone.now()
        fields = ["price", "quantity", "status", "updated_at"]
        updated = 0
        batch = []

        for product in targets.iterator(chunk_size=BATCH_SIZE):
            if not self._fill(product, price, quantity, keep_status, skip_on_order):
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
