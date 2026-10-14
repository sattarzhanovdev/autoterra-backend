"""Разбор Excel-файла с товарами (шаблон Wildberries «Общие характеристики»
и совместимые/упрощённые таблицы, включая иерархический прайс 1С).

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

В прайсе 1С шапка состоит из нескольких строк: «Номенклатура» → name,
«Код» → sku (включая ведущие нули), «Артикул» → wb_article. Строки папок
определяются по outlineLevel даже при наличии кода/агрегатного остатка;
верхний раздел → group_name, категория нормализуется по названию и иерархии
в одну из 12 категорий каталога. Неопределённые категории пропускаются с
предупреждением. Цена округляется до копеек, дробный остаток — вниз до целых
единиц с предупреждением.
Неправильные цены/остатки/идентификаторы не обнуляют товар, а пропускают строку.

Модуль ничего не пишет в БД — только парсит и нормализует строки. Апсерт
делает вызывающий код (admin-импорт и API-эндпоинт).
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP

from openpyxl import load_workbook

from api.models import MAX_PRODUCT_IMAGES, normalize_product_images
from api.services.product_names import normalize_product_synonyms
from api.services.product_categories import determine_product_category


# Каноническое имя поля -> набор допустимых заголовков (в нижнем регистре).
_HEADER_SYNONYMS = {
    "group_name": ["группа", "group"],
    "sku": ["артикул продавца", "артикул", "sku", "код", "код товара", "article"],
    "wb_article": ["артикул wb", "wb", "артикул вб"],
    "name": ["наименование", "название", "name", "product name", "товар", "номенклатура"],
    "category": ["категория продавца", "категория", "category"],
    "brand": ["бренд", "brand", "производитель"],
    "description": ["описание", "description"],
    "synonyms": ["синонимы", "альтернативные названия", "synonyms"],
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
    return " ".join(str("" if value is None else value).strip().split())


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


def _find_1c_header(rows):
    """Combine the 1C report header bands, stopping before nomenclature data.

    Real export: Номенклатура/Артикул/Код on row 3, Цена/Остаток/Фото
    on row 6, with currency and tax notes between them. No positional mapping.
    """
    for start, row in enumerate(rows[:_MAX_HEADER_SCAN]):
        name_col = next((i for i, value in enumerate(row)
                         if _norm(value).lower() == "номенклатура"), None)
        if name_col is None:
            continue
        mapping, code_col, article_col, end = {}, None, None, start
        for index in range(start, min(start + 8, len(rows))):
            band = rows[index]
            name_value = _norm(band[name_col]) if name_col < len(band) else ""
            if index > start and name_value and name_value.lower() != "номенклатура":
                break
            # Later/lower header bands refine the same column, e.g. stock title.
            mapping.update(_map_header_row(band))
            for col, value in enumerate(band):
                header = _norm(value).lower()
                if header in ("код", "код товара"):
                    code_col = col
                elif header == "артикул":
                    article_col = col
            if _map_header_row(band):
                end = index
        if code_col is not None:
            mapping["name"] = name_col
            mapping["sku"] = code_col
            if article_col is not None:
                mapping["wb_article"] = article_col
            return end, mapping
    return None, {}


def _identifier(cell):
    """Keep textual leading zeros and Excel zero-padded numeric codes."""
    value = cell.value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
            result = str(int(value))
            if re.fullmatch(r"0+", cell.number_format or ""):
                result = result.zfill(len(cell.number_format))
            return result
    return _norm(value)


def _one_c_number(value, label):
    try:
        if isinstance(value, bool):
            raise ValueError()
        amount = Decimal(_norm(value).replace(" ", "").replace(",", ".") or "0")
        if not amount.is_finite() or amount < 0:
            raise ValueError()
        return amount
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label}: некорректное неотрицательное число {value!r}") from None


def _one_c_stock(row, mapping):
    values, warnings = {}, []
    if "price" in mapping:
        price = _one_c_number(_cell(row, mapping, "price"), "Цена")
        if price > Decimal("9999999999.99"):
            raise ValueError("Цена превышает допустимый предел")
        values["price"] = price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if "quantity" in mapping:
        quantity = _one_c_number(_cell(row, mapping, "quantity"), "Остаток")
        if quantity > 2147483647:
            raise ValueError("Остаток превышает допустимый предел")
        whole = quantity.to_integral_value(rounding=ROUND_FLOOR)
        values["quantity"] = int(whole)
        if quantity != whole:
            warnings.append(f"дробный остаток {quantity} округлён вниз до {int(whole)} целых единиц")
    return values, warnings


def _one_c_group_rows(sheet, rows, header_idx, mapping):
    """Folders have codes and sometimes aggregate stock: use outline hierarchy.

    A named row followed by a deeper named row is a folder, not a product.
    Flat exports may also have standalone section labels with no product data.
    """
    named = [(index, row) for index, row in enumerate(rows[header_idx + 1:], header_idx + 1)
             if _norm(_cell(row, mapping, "name"))]
    groups = set()
    for (index, row), (next_index, _) in zip(named, named[1:]):
        if sheet.row_dimensions[next_index + 1].outlineLevel > sheet.row_dimensions[index + 1].outlineLevel:
            groups.add(index)
    for index, row in named:
        if all(not _norm(_cell(row, mapping, key)) for key in ("sku", "wb_article", "price", "quantity", "images", "status")):
            groups.add(index)
    return groups


def _validate_one_c_product(product):
    from api.models import Product
    for field, value in product.items():
        if not isinstance(value, str) or field.startswith("_"):
            continue
        limit = Product._meta.get_field(field).max_length
        if limit and len(value) > limit:
            raise ValueError(f"Поле {field}: длина превышает {limit} символов")


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
    raw = str(value or "").strip()
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


def parse_products_workbook(file_obj, *, category_audit=None):
    """Парсит книгу Excel и возвращает (products, errors).

    products — список dict с каноническими ключами и флагами наличия колонок
    (`_has_price`, `_has_quantity`, `_has_status`), чтобы вызывающий код мог
    не затирать цену/остаток отсутствующими данными.
    errors — список строк с проблемами (для показа пользователю).
    category_audit — необязательный список, в который добавляются решения
    классификатора 1С с номером строки, кодом, папкой и полной иерархией.
    Аудит ничего не пишет в БД и не меняет решения классификатора.
    """
    try:
        workbook = load_workbook(file_obj, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 — покажем пользователю понятную ошибку
        return [], [f"Не удалось прочитать файл: {exc}"]

    try:
        # Предпочитаем лист «Товары», иначе первый активный.
        sheet = None
        for name in workbook.sheetnames:
            if _norm(name).lower() in ("товары", "products", "товар"):
                sheet = workbook[name]
                break
        if sheet is None:
            sheet = workbook.active

        # Keep the existing streaming path for WB/plain workbooks. Only 1C
        # needs row_dimensions, which openpyxl read-only sheets do not expose.
        header_rows = list(sheet.iter_rows(max_row=_MAX_HEADER_SCAN, values_only=True))
        if _find_1c_header(header_rows)[0] is not None:
            sheet_name = sheet.title
            workbook.close()
            if hasattr(file_obj, "seek"):
                file_obj.seek(0)
            workbook = load_workbook(file_obj, data_only=True)
            sheet = workbook[sheet_name]
        return _parse_products_sheet(sheet, category_audit=category_audit)
    finally:
        workbook.close()


def _parse_products_sheet(sheet, *, category_audit=None):
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return [], ["Файл пустой."]

    header_idx, mapping = _find_1c_header(rows)
    is_one_c = header_idx is not None
    if not is_one_c:
        if any(_norm(value).lower() == "номенклатура"
               for row in rows[:_MAX_HEADER_SCAN] for value in row):
            return [], ["Не найдена колонка «Код» в шапке прайса 1С — импорт по артикулу запрещён."]
        header_idx, mapping = _find_header(rows)
    if header_idx is None:
        return [], ["Не найдена строка с заголовками (нужны «Код» и «Номенклатура» для 1С или «Артикул продавца» и «Наименование»)."]

    has_price = "price" in mapping
    has_quantity = "quantity" in mapping
    has_status = "status" in mapping

    products, errors = [], []
    group_rows = _one_c_group_rows(sheet, rows, header_idx, mapping) if is_one_c else set()
    groups = {}
    has_outline = is_one_c and any(sheet.row_dimensions[i + 1].outlineLevel for i in range(header_idx + 1, len(rows)))
    for number, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        if is_one_c and any(sheet.cell(number, mapping[field] + 1).data_type == "e"
                            for field in ("sku", "name")):
            errors.append(f"Строка {number}: код или номенклатура содержит ошибку Excel — пропущено.")
            continue
        sku = (_identifier(sheet.cell(number, mapping["sku"] + 1)) if is_one_c
               else _norm(_cell(row, mapping, "sku")))
        name = _norm(_cell(row, mapping, "name"))
        if is_one_c:
            if not name and not sku:
                continue
            if name.lower() == "номенклатура" and sku.lower() in ("код", "код товара"):
                continue  # repeated report header
            level = sheet.row_dimensions[number].outlineLevel if has_outline else (0 if number - 1 in group_rows else 1)
            groups = {depth: label for depth, label in groups.items() if depth < level}
            if number - 1 in group_rows:
                groups[level] = name
                continue
            if not name and sku:
                errors.append(f"Строка {number} ({sku}): нет номенклатуры — пропущено.")
                continue

        # Пустые строки и строки-подсказки (без названия) пропускаем молча.
        if not name:
            continue
        if not sku:
            identifier = "кода" if is_one_c else "артикула"
            errors.append(f"Строка {number}: указано название, но нет {identifier} — пропущено.")
            continue

        stock_values, stock_warnings = {}, []
        if is_one_c:
            try:
                stock_values, stock_warnings = _one_c_stock(row, mapping)
            except (ValueError, InvalidOperation) as exc:
                errors.append(f"Строка {number} ({sku}): {exc} — пропущено.")
                continue

        images, image_warnings = _images(_cell(row, mapping, "images"))
        for warning in image_warnings + stock_warnings:
            errors.append(f"Строка {number} ({sku}): {warning}.")

        product = {
            "sku": sku,
            "name": name,
            "wb_article": _norm(_cell(row, mapping, "wb_article")),
            "group_name": _norm(_cell(row, mapping, "group_name")),
            "category": _norm(_cell(row, mapping, "category")) or "Без категории",
            "brand": _norm(_cell(row, mapping, "brand")) or "AutoTerra",
            "_has_brand": "brand" in mapping and bool(_norm(_cell(row, mapping, "brand"))),
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
        if is_one_c:
            if "wb_article" in mapping:
                product["wb_article"] = _identifier(sheet.cell(number, mapping["wb_article"] + 1))
            labels = [groups[depth] for depth in sorted(groups)]
            if labels and "group_name" not in mapping:
                product["group_name"] = labels[0]
            product.update(stock_values)
            folder = _norm(_cell(row, mapping, "category")) or (labels[-1] if labels else product["group_name"])
            category = determine_product_category(name, folder, [product["group_name"], *labels])
            # Validate normalized fields before auditing; unknown categories
            # are never saved, but their otherwise valid rows must be reported.
            product["category"] = category or ""
            try:
                _validate_one_c_product(product)
            except ValueError as exc:
                errors.append(f"Строка {number} ({sku}): {exc} — пропущено.")
                continue
            if category_audit is not None:
                hierarchy = labels or ([product["group_name"]] if product["group_name"] else [])
                category_audit.append({
                    "row": number, "sku": sku, "name": name,
                    "folder": folder, "hierarchy": hierarchy, "category": category,
                })
            if category is None:
                errors.append(
                    f"Строка {number} ({sku}): не удалось определить категорию товара «{name}» "
                    f"(папка 1С: «{folder}») — пропущено; "
                    "нужна одна из 12 категорий каталога."
                )
                continue
        if "synonyms" in mapping:
            product["synonyms"] = normalize_product_synonyms(_norm(_cell(row, mapping, "synonyms")))
        if "images" not in mapping:
            product.pop("images")
        if has_price and not is_one_c:
            product["price"] = _money(_cell(row, mapping, "price", 0))
        if has_quantity and not is_one_c:
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
    "color", "barcode", "images", "synonyms", "video_url", "volume", "weight",
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
        if existing is not None and not item.get("_has_brand", "brand" in item):
            defaults.pop("brand", None)

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
