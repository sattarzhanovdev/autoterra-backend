"""Regression fixtures reproduce the bands/columns/outline of the real 1C price.

No commercial workbook is stored in Git. Its A/M/N/O/Q/S layout and folder
outline (folders also carry codes and aggregate stock) are reproduced below.
"""
import io
from decimal import Decimal

from django.test import TestCase
from openpyxl import Workbook

from .models import MAX_PRODUCT_IMAGES, Distributor, Product
from .services.product_import import parse_products_workbook
from .test_product_import import StockUploadFileEndpointTests


def one_c_workbook(rows=None, *, photo_header=True):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Лист_1'
    sheet['A1'] = 'Прайс-лист на 8 октября 2026 г.'
    for col, value in {'A': 'Номенклатура', 'M': 'Артикул', 'N': 'Код', 'Q': '*Основной склад (единый)'}.items():
        sheet[f'{col}3'] = value
    sheet['O4'], sheet['O5'] = 'руб.', 'Включает НДС'
    for col, value in {'O': 'Цена', 'P': 'Валюта', 'Q': 'Остаток', 'R': 'Наличие',
                       'T': 'Источник фото', 'U': 'Статус фото', 'V': 'Кандидат фото'}.items():
        sheet[f'{col}6'] = value
    if photo_header:
        sheet['S6'] = 'Фото'
    if rows is None:
        rows = [
            {'name': '000.Akzo Nobel', 'code': '00023179', 'level': 0},
            {'name': 'Akzo Nobel Оборудование', 'code': '00023484', 'level': 1, 'price': 0, 'quantity': 3},
            {'name': '2002956 Спектрофотометр Automatchic Vision', 'article': '2002956', 'code': '00023078',
             'price': 264471.894, 'quantity': 2, 'status': 'Под заказ', 'images': 'https://example.com/photo.jpg',
             'candidate': 'https://example.com/candidate.jpg', 'level': 2},
        ]
    columns = {'name': 'A', 'article': 'M', 'code': 'N', 'price': 'O', 'quantity': 'Q',
               'status': 'R', 'images': 'S', 'candidate': 'V'}
    for number, row in enumerate(rows, 7):
        sheet.row_dimensions[number].outlineLevel = row.get('level', 0)
        for field, col in columns.items():
            if field in row:
                sheet[f'{col}{number}'] = row[field]
        sheet[f'N{number}'].number_format = row.get('code_format', 'General')
    stream = io.BytesIO()
    workbook.save(stream)
    stream.seek(0)
    stream.name = '1c-price.xlsx'
    return stream


def item(**changes):
    result = {'name': 'Грунт', 'article': 'ARTICLE-1', 'code': '000001', 'price': 100,
              'quantity': 5, 'images': 'https://example.com/a.jpg', 'level': 1}
    result.update(changes)
    return result


class OneCParserTests(TestCase):
    def test_real_multilevel_header_name_code_article_price_stock_and_photo(self):
        products, errors = parse_products_workbook(one_c_workbook())
        self.assertEqual(errors, [])
        self.assertEqual(len(products), 1)  # coded folders must not become products
        product = products[0]
        self.assertEqual(product['name'], '2002956 Спектрофотометр Automatchic Vision')
        self.assertEqual(product['sku'], '00023078')
        self.assertEqual(product['wb_article'], '2002956')
        self.assertEqual(product['price'], Decimal('264471.89'))
        self.assertEqual(product['quantity'], 2)
        self.assertEqual(product['images'], ['https://example.com/photo.jpg'])
        self.assertEqual(product['group_name'], '000.Akzo Nobel')
        self.assertEqual(product['category'], 'Akzo Nobel Оборудование')
        self.assertEqual(product['status'], 'onOrder')
        self.assertTrue(product['_has_price'])
        self.assertTrue(product['_has_quantity'])

    def test_nested_folders_aggregate_stock_and_sibling_categories(self):
        rows = [
            {'name': 'Материалы', 'code': 'G0', 'level': 0},
            {'name': 'Лаки', 'code': 'G1', 'level': 1, 'price': 0, 'quantity': 100},
            {'name': '2K', 'code': 'G2', 'level': 2, 'price': 0, 'quantity': 99},
            item(name='Лак', code='L1', level=3),
            {},  # empty separators do not erase the current folder
            item(name='Лак без артикула', article='', code='L2', level=3),
            {'name': 'Грунты', 'code': 'G3', 'level': 1},
            item(code='P1', level=2),
            {'name': 'Инструменты', 'code': 'G4', 'level': 0},
            item(code='T1', level=1),
        ]
        products, errors = parse_products_workbook(one_c_workbook(rows))
        self.assertEqual(errors, [])
        self.assertEqual([p['sku'] for p in products], ['L1', 'L2', 'P1', 'T1'])
        self.assertEqual([p['category'] for p in products], ['2K', '2K', 'Грунты', 'Инструменты'])
        self.assertEqual([p['group_name'] for p in products], ['Материалы'] * 3 + ['Инструменты'])
        self.assertEqual(products[1]['wb_article'], '')

    def test_fractional_stock_floors_with_row_warning_and_localized_numbers(self):
        products, errors = parse_products_workbook(one_c_workbook([
            item(price='1\u00a0234,565', quantity='2,656'),
            item(code='000002', price=0.609, quantity=0.971),
        ]))
        self.assertEqual([p['price'] for p in products], [Decimal('1234.57'), Decimal('0.61')])
        self.assertEqual([p['quantity'] for p in products], [2, 0])
        self.assertEqual(len(errors), 2)
        self.assertIn('Строка 7 (000001)', errors[0])
        self.assertIn('округлён вниз до 2', errors[0])
        self.assertIn('округлён вниз до 0', errors[1])

    def test_invalid_rows_skip_without_stopping_valid_rows(self):
        bad = [
            item(code=None), item(code='BAD-NAME', name=None), item(code='BAD-PRICE', price='не число'),
            item(code='NEGATIVE', quantity=-1), item(code='INFINITY', price='Infinity'),
            item(code='NAN', quantity='NaN'), item(code='BIG-PRICE', price='10000000000'),
            item(code='BIG-QTY', quantity=2147483648), item(code='LONG-CODE-' + 'x' * 65),
            item(code='LONG-NAME', name='x' * 256),
            item(code='#REF!'), item(code='ERROR-NAME', name='#DIV/0!'),
        ]
        products, errors = parse_products_workbook(one_c_workbook(bad + [item(code='GOOD')]))
        self.assertEqual([p['sku'] for p in products], ['GOOD'])
        self.assertEqual(len(errors), len(bad))
        self.assertTrue(all('пропущено' in warning for warning in errors))
        self.assertIn('нет кода', errors[0])

    def test_multiple_photos_newlines_duplicates_invalid_urls_and_limit(self):
        links = [f'https://example.com/{i}.jpg' for i in range(20)]
        value = '\n'.join(links) + ';' + links[0] + ',ftp://example.com/not-a-photo'
        products, errors = parse_products_workbook(one_c_workbook([item(images=value)]))
        self.assertEqual(products[0]['images'], links[:MAX_PRODUCT_IMAGES])
        self.assertEqual(len(errors), 2)
        self.assertTrue(any('сохранены первые 15' in warning for warning in errors))
        self.assertTrue(any('без http(s)-ссылки' in warning for warning in errors))

    def test_numeric_code_zero_padding_and_zero_stock(self):
        products, errors = parse_products_workbook(one_c_workbook([
            item(code=123, code_format='00000000', article=987, quantity=0),
            item(code=0, article='000ABC', quantity=1),
        ]))
        self.assertEqual(errors, [])
        self.assertEqual(products[0]['sku'], '00000123')
        self.assertEqual(products[0]['wb_article'], '987')
        self.assertEqual(products[1]['sku'], '0')
        self.assertEqual(products[1]['wb_article'], '000ABC')
        self.assertEqual(products[0]['quantity'], 0)

    def test_missing_photo_column_does_not_clear_images(self):
        products, errors = parse_products_workbook(one_c_workbook(photo_header=False))
        self.assertEqual(errors, [])
        self.assertNotIn('images', products[0])

    def test_flat_one_c_and_column_order_are_detected_by_names(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(['Фото', 'Артикул', 'Остаток', 'Номенклатура', 'Цена', 'Код'])
        sheet.append(['https://example.com/a.jpg', 'SUPPLIER-1', 4, 'Лак', 350.5, '000CODE'])
        stream = io.BytesIO()
        workbook.save(stream)
        stream.seek(0)
        products, errors = parse_products_workbook(stream)
        self.assertEqual(errors, [])
        self.assertEqual(products[0]['sku'], '000CODE')
        self.assertEqual(products[0]['wb_article'], 'SUPPLIER-1')
        self.assertEqual(products[0]['images'], ['https://example.com/a.jpg'])

    def test_one_c_without_code_header_does_not_fall_back_to_article(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(['Номенклатура', 'Артикул', 'Цена', 'Остаток'])
        sheet.append(['Лак', 'SUPPLIER', 100, 2])
        stream = io.BytesIO()
        workbook.save(stream)
        stream.seek(0)
        products, errors = parse_products_workbook(stream)
        self.assertEqual(products, [])
        self.assertIn('«Код»', errors[0])

    def test_ordinary_excel_keeps_article_as_sku(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(['Артикул', 'Код', 'Наименование', 'Цена', 'Остаток'])
        sheet.append(['LEGACY', 'EXTERNAL', 'Лак', 200, 3])
        stream = io.BytesIO()
        workbook.save(stream)
        stream.seek(0)
        products, errors = parse_products_workbook(stream)
        self.assertEqual(errors, [])
        self.assertEqual(products[0]['sku'], 'LEGACY')


class OneCUploadEndpointTests(TestCase):
    setUp = StockUploadFileEndpointTests.setUp
    _upload = StockUploadFileEndpointTests._upload

    def test_repeat_import_updates_same_distributor_code_and_catalog_images(self):
        existing = Product.objects.create(distributor=self.distributor, sku='00023078', name='Старое название',
            category='Старый раздел', brand='Бренд', price=1, quantity=99)
        other = Distributor.objects.create(name='Другой', inn='other-import-1c')
        foreign = Product.objects.create(distributor=other, sku='00023078', name='Чужой товар',
            category='Категория', price=10, quantity=11)
        for _ in range(2):
            response = self._upload(one_c_workbook())
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(response.json()['created'], 0)
            self.assertEqual(response.json()['updated'], 1)
            self.assertEqual(response.json()['processed'], 1)
            existing.refresh_from_db()
            self.assertEqual(existing.name, '2002956 Спектрофотометр Automatchic Vision')
            self.assertEqual(existing.price, Decimal('264471.89'))
            self.assertEqual(existing.quantity, 2)
            self.assertEqual(existing.wb_article, '2002956')
            self.assertEqual(existing.brand, 'Бренд')
        self.assertEqual(Product.objects.filter(distributor=self.distributor).count(), 1)
        foreign.refresh_from_db()
        self.assertEqual(foreign.price, Decimal('10'))
        self.assertEqual(foreign.quantity, 11)
        auth = {'HTTP_AUTHORIZATION': f'Bearer {self.token.key}'}
        response = self.http.get('/api/distributor/stock/', **auth)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['results'][0]['images'], ['https://example.com/photo.jpg'])

    def test_reimport_changed_price_stock_article_photos_without_duplicates(self):
        response = self._upload(one_c_workbook())
        self.assertEqual(response.json()['created'], 1)
        product = Product.objects.get(distributor=self.distributor, sku='00023078')
        response = self._upload(one_c_workbook([item(code='00023078', article='NEW-ARTICLE', name='Новое название',
            price='123,45', quantity=7, images='https://example.com/new.jpg')]))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['created'], 0)
        self.assertEqual(response.json()['updated'], 1)
        product.refresh_from_db()
        self.assertEqual(product.name, 'Новое название')
        self.assertEqual(product.price, Decimal('123.45'))
        self.assertEqual(product.quantity, 7)
        self.assertEqual(product.wb_article, 'NEW-ARTICLE')
        self.assertEqual(product.images, ['https://example.com/new.jpg'])
        self.assertEqual(Product.objects.filter(distributor=self.distributor).count(), 1)

    def test_bad_rows_and_fractional_warnings_are_returned_by_endpoint(self):
        response = self._upload(one_c_workbook([item(code='BAD', price='ошибка'), item(quantity='0,971')]))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['created'], 1)
        self.assertEqual(len(response.json()['errors']), 2)
        self.assertFalse(Product.objects.filter(sku='BAD').exists())
        self.assertEqual(Product.objects.get(sku='000001').quantity, 0)

    def test_missing_photo_column_preserves_existing_photos_on_update(self):
        original = Product.objects.create(distributor=self.distributor, sku='00023078', name='Товар',
            category='Категория', images=['https://example.com/preserved.jpg'])
        response = self._upload(one_c_workbook(photo_header=False))
        self.assertEqual(response.status_code, 200, response.content)
        original.refresh_from_db()
        self.assertEqual(original.images, ['https://example.com/preserved.jpg'])
