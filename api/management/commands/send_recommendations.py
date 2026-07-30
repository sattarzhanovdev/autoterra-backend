"""Рассылка персональных AI-рекомендаций (п. 13 ТЗ).

Запускается по расписанию — раз в сутки достаточно:

    python manage.py send_recommendations              # что уйдёт (без отправки)
    python manage.py send_recommendations --apply
    python manage.py send_recommendations --apply --rules dormant,repeat_sku

Повторы гасит окно тишины: одна и та же подсказка не уходит клиенту чаще, чем
раз в --cooldown дней.
"""

from django.core.management.base import BaseCommand

from api.services import recommendations as rec


class Command(BaseCommand):
    help = "Формирует и рассылает персональные рекомендации клиентам"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Отправить. Без флага команда только показывает, что уйдёт.",
        )
        parser.add_argument(
            "--rules",
            default="",
            help=f"Через запятую: {', '.join(rec.ALL_RULES)}. Пусто — все.",
        )
        parser.add_argument(
            "--cooldown",
            type=int,
            default=rec.DEFAULT_COOLDOWN_DAYS,
            help="Через сколько дней можно повторить ту же подсказку.",
        )
        parser.add_argument(
            "--skip-region-tasks",
            action="store_true",
            help="Не создавать задачи менеджерам по «тихим» регионам.",
        )

    def handle(self, *args, **options):
        requested = [key.strip() for key in options["rules"].split(",") if key.strip()]
        unknown = [key for key in requested if key not in rec.ALL_RULES]
        if unknown:
            self.stderr.write(self.style.ERROR(f"Неизвестные правила: {', '.join(unknown)}"))
            return

        items = rec.collect(requested or None)
        self.stdout.write(f"Подсказок сформировано: {len(items)}")

        by_rule = {}
        for item in items:
            by_rule.setdefault(item.rule, []).append(item)
        for rule, group in sorted(by_rule.items()):
            self.stdout.write(f"  {rule}: {len(group)}")

        if items:
            self.stdout.write("")
            self.stdout.write("Примеры (первые 5):")
            for item in items[:5]:
                self.stdout.write(f"  → {item.user}: [{item.title}] {item.body[:80]}")

        stats = rec.send(items, cooldown_days=options["cooldown"], dry_run=not options["apply"])

        if not options["skip_region_tasks"]:
            if options["apply"]:
                tasks = rec.low_activity_regions()
                self.stdout.write(f"Задач менеджерам по тихим регионам: {len(tasks)}")
            else:
                self.stdout.write("Задачи по тихим регионам будут созданы при --apply")

        self.stdout.write("")
        if options["apply"]:
            self.stdout.write(self.style.SUCCESS(
                f"Отправлено: {stats['sent']}, пропущено как повтор: {stats['skipped']}"
            ))
        else:
            self.stdout.write(self.style.WARNING(
                f"Пробный прогон: отправилось бы {stats['sent']}, "
                f"пропущено как повтор {stats['skipped']}. Повторите с --apply."
            ))
