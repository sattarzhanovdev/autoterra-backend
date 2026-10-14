"""Read-only category audit of a private 1C workbook; no database writes."""
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from api.services.product_categories import CANONICAL_PRODUCT_CATEGORIES
from api.services.product_import import parse_products_workbook


def _table(rows, fields):
    def escape(value):
        if isinstance(value, list):
            value = ' → '.join(value)
        return str(value if value is not None else '').replace('|', '\\|').replace('\n', ' ')
    return '\n'.join([
        '| ' + ' | '.join(fields) + ' |',
        '| ' + ' | '.join('---' for _ in fields) + ' |',
        *('| ' + ' | '.join(escape(row.get(field)) for field in fields) + ' |' for row in rows),
    ])


class Command(BaseCommand):
    help = 'Аудит категорий прайса 1С без записи в БД: JSON, CSV и Markdown.'

    def add_arguments(self, parser):
        parser.add_argument('workbook', type=Path)
        parser.add_argument('--output-dir', required=True, type=Path)
        parser.add_argument('--baseline', type=Path, help='JSON аудита до изменения классификатора')
        parser.add_argument('--expect-total', type=int)
        parser.add_argument('--require-complete', action='store_true')

    def handle(self, *args, **options):
        source = options['workbook']
        if not source.is_file():
            raise CommandError('Файл прайса не найден')
        audit = []
        products, warnings = parse_products_workbook(source, category_audit=audit)
        if not audit:
            raise CommandError('Не найдено валидных строк формата 1С')
        if any(product['category'] not in CANONICAL_PRODUCT_CATEGORIES for product in products):
            raise CommandError('Классификатор вернул неканоническую категорию')
        folder_totals = Counter(row['folder'] for row in audit)
        unresolved = [row for row in audit if row['category'] is None]
        baseline = []
        if options['baseline']:
            raw = json.loads(options['baseline'].read_text(encoding='utf-8'))
            baseline = raw['products'] if isinstance(raw, dict) else raw
        by_key = {(row['sku'], row['name']): row for row in audit}
        before = []
        for row in baseline:
            if row.get('baseline_category', row.get('category')) is not None:
                continue
            current = by_key.get((row['sku'], row['name']))
            if current is None:
                raise CommandError(f"Строка исходного аудита отсутствует: {row['sku']}")
            before.append(dict(current))
        before_totals = Counter(row['folder'] for row in before)
        for row in audit:
            row['folder_total'] = folder_totals[row['folder']]
        for row in before:
            row['folder_total'] = folder_totals[row['folder']]
            row['folder_unresolved_before'] = before_totals[row['folder']]
        distribution = {category: sum(p['category'] == category for p in products)
                        for category in CANONICAL_PRODUCT_CATEGORIES}
        summary = {
            'source': source.name, 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
            'total_product_rows': len(audit), 'classified': len(products),
            'unresolved': len(unresolved), 'unresolved_before': len(before),
            'distribution': distribution, 'warnings': warnings,
            'folder_counts_before': dict(before_totals.most_common()),
        }
        output = options['output_dir']
        output.mkdir(parents=True, exist_ok=True)
        (output / 'category-audit.json').write_text(json.dumps(
            dict(summary=summary, products=audit, unresolved=unresolved, unresolved_before=before),
            ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        fields = ['row', 'sku', 'name', 'folder', 'hierarchy', 'folder_total',
                  'folder_unresolved_before', 'category']
        with (output / 'unresolved-before.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for row in before:
                writer.writerow({field: (' → '.join(row[field]) if field == 'hierarchy' else row.get(field, ''))
                                 for field in fields})
        report = [
            '# Аудит категорий реального прайса 1С', '',
            f"Файл: `{source.name}`. SHA-256: `{summary['source_sha256']}`.", '',
            f'Строк товаров: {len(audit)}; классифицировано: {len(products)}; unresolved: {len(unresolved)}.',
            f'До расширения: unresolved {len(before)}. Аудит не изменяет БД.', '',
            '## Распределение', '',
            _table([dict(category=key, count=value) for key, value in distribution.items()], ['category', 'count']),
            '', '## Папки, содержащие unresolved до расширения', '',
            _table([dict(folder=folder, unresolved_before=count, folder_total=folder_totals[folder])
                    for folder, count in before_totals.most_common()], ['folder', 'unresolved_before', 'folder_total']),
            '', '## Все unresolved до расширения и итоговая категория', '',
            _table(before, fields), '', '## Неоднозначные позиции после расширения', '',
            _table(unresolved, ['row', 'sku', 'name', 'folder', 'hierarchy']) if unresolved else 'Нет.',
            '', '## Другие предупреждения импорта', '',
            f'Всего предупреждений: {len(warnings)}. Полные сообщения сохранены в category-audit.json.',
            'Дробные остатки и обработка фотографий не менялись.', '',
        ]
        (output / 'CATEGORY_AUDIT.md').write_text('\n'.join(report), encoding='utf-8')
        self.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2))
        if options['expect_total'] is not None and len(audit) != options['expect_total']:
            raise CommandError(f"Ожидалось {options['expect_total']} товаров, найдено {len(audit)}")
        if options['require_complete'] and unresolved:
            raise CommandError(f'Остались неклассифицированные товары: {len(unresolved)}')
