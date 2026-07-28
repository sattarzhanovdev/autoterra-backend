"""Разбор Excel-файла с товарами (шаблон Wildberries «Общие характеристики»
и совместимые/упрощённые таблицы).

Особенности шаблона WB, которые здесь учитываются:
  • Лист с товарами называется «Товары» (может быть не первым/активным).
  • Шапка расположена не в первой строке (несколько строк с названиями
    секций и подсказками сверху) — строку с заголовками находим по названиям
    колонок.
  • Колонки идентифицируются ПО НАЗВАНИЮ, а не по позиции.
  • Есть строка-подсказка сразу под шапкой — её пропускаем (нет названия).
  • Файл может не содержать колонок с ценой и остатком — тогда для
    существующих товаров цена/остаток НЕ перезаписываются.
  • В колонке «Фото» лежат ССЫЛКИ на изображения через ';' (в шаблоне WB их
    допускается до 30) — самих файлов в книге нет. Мы сохраняем только ссылки
    и не более MAX_PRODUCT_IMAGES штук на товар.

Модуль ничего не пишет в БД — только парсит и нормализует строки. Апсерт
делает вызывающий код (admin-импорт и API-эндпоинт).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from openpyxl import load_workbook

from api.models import MAX_PRODUCT_IMAGES, normalize_product_images


# Каноническое имя поля -> набор допустимых заголовков (в нижнем регистре).
_HEADER_SYNONYMS = {
    "group_name": ["группа", "group"],
    "sku": ["артикул продавца", "артикул", "sku", "код", "код товара", "article"],
    "wb_article": ["артикул wb", "wb", "артикул вб"],
    "name": ["наименование", "название", "name", "product name", "товар"],
    "category": ["категория продавца", "категория", "category"],
    "brand": ["бренд", "brand", "производитель"],
    "description": ["описание", "description"],
    "images": ["фото", "фотографии", "images", "изображения"],
    "video_url": ["видео", "video"],
    "color": ["цвет", "color"],
    "barcode": ["баркод", "штрихкод", "штрих-код", "barcode"],
    "volume": ["объем", "объём", "volume", "фасовка", "size"],
    "weight": ["вес с упаковкой (кг)", "вес с упаковкой", "вес", "weight"],
    "package_height": ["высота упаковки", "высота", "height"],
    "package_length": ["длина упаковки", "длина", "length"],
    "package_width": ["ширина упаковки", "ширина", "width"],
    "tnved": ["тнвэд", "тн вэд", "tnved", "hs code"],
    "vat_rate": ["ставка ндс", "ндс", "vat"],
    "price": ["цена", "price", "стоимость"],
    "quantity": ["остаток", "количество", "quantity", "qty", "stock", "balance", "кол-во"],
    "status": ["статус", "status", "наличие"],
}

# Обратный индекс: заголовок -> каноническое имя.
_HEADER_LOOKUP = {}
for _canon, _aliases in _HEADER_SYNONYMS.items():
    for _alias in _aliases:
        _HEADER_LOOKUP.setdefault(_alias, _canon)

_REQUIRED = {"sku", "name"}
_MAX_HEADER_SCAN = 20  # сколько верхних строк просматриваем в поисках шапки

_STATUS_ALIASES = {
    "instock": "inStock", "in stock": "inStock", "в наличии": "inStock", "наличие": "inStock",
    "low": "low", "мало": "low", "заканчивается": "low",
    "onorder": "onOrder", "on order": "onOrder", "под заказ": "onOrder",
    "outofstock": "outOfStock", "out of stock": "outOfStock", "нет": "outOfStock", "нет в наличии": "outOfStock",
}


def _norm(value) -> str:
    return " ".join(str(value or "").strip().split())


def _map_header_row(row):
    """Строит {каноническое_поле: индекс_колонки} для строки-кандидата шапки."""
    mapping = {}
    for index, cell in enumerate(row):
        key = _HEADER_LOOKUP.get(_norm(cell).lower())
        if key and key not in mapping:
            mapping[key] = index
    return mapping


def _find_header(rows):
    """Возвращает (индекс_строки_шапки, mapping) или (None, {}).

    Выбираем строку с наибольшим числом распознанных колонок при условии, что
    в ней есть обязательные sku и name.
    """
    best_idx, best_map = None, {}
    for idx, row in enumerate(rows[:_MAX_HEADER_SCAN]):
        mapping = _map_header_row(row)
        if _REQUIRED.issubset(mapping) and len(mapping) > len(best_map):
            best_idx, best_map = idx, mapping
    return best_idx, best_map


def _cell(row, mapping, key, default=None):
    index = mapping.get(key)
    if index is None or index >= len(row):
        return default
    value = row[index]
    return default if value is None else value


def _money(value):
    try:
        return Decimal(_norm(value).replace(" ", "").replace(",", ".") or "0")
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _int(value):
    try:
        return int(_money(value))
    except (InvalidOperation, ValueError):
        return 0


def _status(value, default="inStock"):
    text = _norm(value).lower()
    return _STATUS_ALIASES.get(text, default)


def _images(value):
    """Разбирает ячейку «Фото» → (ссылки, предупреждения).

    WB разделяет ссылки ';' (допускаем ',' и перенос строки). Дубликаты и
    значения без http(s) отбрасываем, сверх MAX_PRODUCT_IMAGES — обрезаем и
    сообщаем об этом вызывающему коду, чтобы предупредить пользователя.
    """
    raw = _norm(value)
    if not raw:
        return [], []

    valid, skipped = [], 0
    for chunk in raw.replace("\n", ";").replace(",", ";").split(";"):
        url = chunk.strip()
        if not url:
            continue
        if not url.lower().startswith(("http://", "https://")):
            skipped += 1
            continue
        if url not in valid:
            valid.append(url)

    warnings = []
    if skipped:
        warnings.append(f"пропущено значений в колонке «Фото» без http(s)-ссылки: {skipped}")
    if len(valid) > MAX_PRODUCT_IMAGES:
        warnings.append(f"фото {len(valid)} шт. — сохранены первые {MAX_PRODUCT_IMAGES}")
    return normalize_product_images(valid), warnings


def parse_products_workbook(file_obj):
    """Парсит книгу Excel и возвращает (products, errors).

    products — список dict с каноническими ключами и флагами наличия колонок
    (`_has_price`, `_has_quantity`, `_has_status`), чтобы вызывающий код мог
    не затирать цену/остаток отсутствующими данными.
    errors — список строк с проблемами (для показа пользователю).
    """
    try:
        workbook = load_workbook(file_obj, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 — покажем пользователю понятную ошибку
        return [], [f"Не удалось прочитать файл: {exc}"]

    # Предпочитаем лист «Товары», иначе первый активный.
    sheet = None
    for name in workbook.sheetnames:
        if _norm(name).lower() in ("товары", "products", "товар"):
            sheet = workbook[name]
            break
    if sheet is None:
        sheet = workbook.active

    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return [], ["Файл пустой."]

    header_idx, mapping = _find_header(rows)
    if header_idx is None:
        return [], ["Не найдена строка с заголовками (нужны колонки «Артикул продавца» и «Наименование»)."]

    has_price = "price" in mapping
    has_quantity = "quantity" in mapping
    has_status = "status" in mapping

    products, errors = [], []
    for number, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        sku = _norm(_cell(row, mapping, "sku"))
        name = _norm(_cell(row, mapping, "name"))

        # Пустые строки и строки-подсказки (без названия) пропускаем молча.
        if not name:
            continue
        if not sku:
            errors.append(f"Строка {number}: указано название, но нет артикула — пропущено.")
            continue

        images, image_warnings = _images(_cell(row, mapping, "images"))
        for warning in image_warnings:
            errors.append(f"Строка {number} ({sku}): {warning}.")

        product = {
            "sku": sku,
            "name": name,
            "wb_article": _norm(_cell(row, mapping, "wb_article")),
            "group_name": _norm(_cell(row, mapping, "group_name")),
            "category": _norm(_cell(row, mapping, "category")) or "Без категории",
            "brand": _norm(_cell(row, mapping, "brand")) or "AutoTerra",
            "description": _norm(_cell(row, mapping, "description")),
            "color": _norm(_cell(row, mapping, "color")),
            "barcode": _norm(_cell(row, mapping, "barcode")),
            "images": images,
            "video_url": _norm(_cell(row, mapping, "video_url")),
            "volume": _money(_cell(row, mapping, "volume", 0)),
            "weight": _money(_cell(row, mapping, "weight", 0)),
            "package_height": _money(_cell(row, mapping, "package_height", 0)),
            "package_length": _money(_cell(row, mapping, "package_length", 0)),
            "package_width": _money(_cell(row, mapping, "package_width", 0)),
            "tnved": _norm(_cell(row, mapping, "tnved")),
            "vat_rate": _norm(_cell(row, mapping, "vat_rate")),
            "_has_price": has_price,
            "_has_quantity": has_quantity,
            "_has_status": has_status,
        }
        if has_price:
            product["price"] = _money(_cell(row, mapping, "price", 0))
        if has_quantity:
            product["quantity"] = max(_int(_cell(row, mapping, "quantity", 0)), 0)
        if has_status:
            product["status"] = _status(_cell(row, mapping, "status", "inStock"))

        products.append(product)

    if not products and not errors:
        errors.append("В файле не найдено ни одной строки с товаром.")

    return products, errors


# Каноническое имя поля -> имя поля модели Product (совпадают, кроме служебных).
_MODEL_FIELDS = (
    "sku", "name", "wb_article", "group_name", "category", "brand", "description",
    "color", "barcode", "images", "video_url", "volume", "weight",
    "package_height", "package_length", "package_width", "tnved", "vat_rate",
)


def upsert_products(distributor, products):
    """Создаёт/обновляет товары дистрибьютора. Возвращает (created, updated).

    Характеристики всегда обновляются. Цена/остаток/статус обновляются только
    если соответствующая колонка присутствовала в файле — иначе существующие
    значения сохраняются, а у новых товаров ставятся безопасные значения по
    умолчанию (цена 0, остаток 0, статус «нет в наличии»).
    """
    from api.models import Product

    created = updated = 0
    for item in products:
        defaults = {field: item[field] for field in _MODEL_FIELDS if field != "sku" and field in item}

        existing = Product.objects.filter(distributor=distributor, sku=item["sku"]).first()

        if item.get("_has_price"):
            defaults["price"] = item["price"]
        elif existing is None:
            defaults["price"] = Decimal("0")

        if item.get("_has_quantity"):
            defaults["quantity"] = item["quantity"]
            if not item.get("_has_status"):
                qty = item["quantity"]
                defaults["status"] = "inStock" if qty > 5 else ("low" if qty > 0 else "outOfStock")
        elif existing is None:
            defaults["quantity"] = 0

        if item.get("_has_status"):
            defaults["status"] = item["status"]
        elif existing is None and "status" not in defaults:
            defaults["status"] = "outOfStock"

        if existing is None:
            defaults["is_active"] = True

        _, was_created = Product.objects.update_or_create(
            distributor=distributor, sku=item["sku"], defaults=defaults,
        )
        created += 1 if was_created else 0
        updated += 0 if was_created else 1

    return created, updated
