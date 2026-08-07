"""Сжигает бонусы клиентов, которые за месяц закупились ниже минимума.

По условиям программы пригласивший должен быть активен сам: если за
календарный месяц его собственные подтверждённые закупки не дотянули до
REFERRAL_ACTIVITY_MIN (по умолчанию 10 000 ₽), весь накопленный бонусный
баланс сгорает.

Использование:
    python manage.py expire_referral_bonuses               # что сгорит (без записи)
    python manage.py expire_referral_bonuses --apply       # сжечь
    python manage.py expire_referral_bonuses --apply --month 2026-07

Без --month берётся предыдущий календарный месяц: команда рассчитана на
запуск по крону в первых числах.

Повторный запуск за тот же месяц ничего не делает — в реестре остаётся отметка
о сгорании, и второй раз оно не проводится.
"""

from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from api.models import ClientProfile
from api.services.bonuses import balance
from api.services.referral_bonus import activity_min, expire_if_inactive, monthly_purchases


class Command(BaseCommand):
    help = "Сжигает бонусы клиентов, не набравших месячный минимум закупок"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Записать изменения. Без этого флага команда только показывает найденное.",
        )
        parser.add_argument(
            "--month",
            help="Месяц в формате ГГГГ-ММ. По умолчанию — предыдущий календарный месяц.",
        )

    def handle(self, *args, **options):
        year, month = self._resolve_month(options.get("month"))
        minimum = activity_min()

        self.stdout.write(f"Месяц: {month:02d}.{year}, минимум закупки: {minimum} ₽")
        self.stdout.write("")

        # Смотрим только тех, кому есть что терять.
        candidates = [c for c in ClientProfile.objects.all() if balance(c) > 0]
        if not candidates:
            self.stdout.write(self.style.SUCCESS("Ни у кого нет положительного баланса."))
            return

        burning = []
        for client in candidates:
            bought = monthly_purchases(client, year, month)
            if bought < minimum:
                burning.append((client, balance(client), bought))

        self.stdout.write(f"С бонусами на счету: {len(candidates)}")
        self.stdout.write(f"Не набрали минимум: {len(burning)}")
        self.stdout.write("")

        for client, amount, bought in burning:
            self.stdout.write(
                f"  #{client.pk} {client.company_name}: сгорит {amount} ₽ "
                f"(закупил за месяц {bought} ₽)"
            )

        if not burning:
            self.stdout.write(self.style.SUCCESS("Сжигать нечего."))
            return

        if not options["apply"]:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING("Пробный прогон: ничего не изменено. Повторите с флагом --apply.")
            )
            return

        total = sum(
            (expire_if_inactive(client, year, month) for client, _, _ in burning),
            start=0,
        )
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Сгорело всего: {total} ₽ у {len(burning)} клиентов"))

    def _resolve_month(self, raw):
        if not raw:
            today = timezone.localdate()
            first = today.replace(day=1)
            previous = first - timezone.timedelta(days=1)
            return previous.year, previous.month
        try:
            year, month = raw.split("-")
            parsed = date(int(year), int(month), 1)
        except (ValueError, TypeError):
            raise CommandError("Месяц указывается как ГГГГ-ММ, например 2026-07")
        return parsed.year, parsed.month
