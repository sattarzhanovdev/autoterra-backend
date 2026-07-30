"""Пересчёт рангов клиентов по обороту.

Ранг меняется сам при подтверждении закупки и оплате заказа. Команда нужна
после правки порогов в админке, после импорта из 1С и для разовой сверки.

Использование:
    python manage.py recalc_partner_tiers                    # что изменится
    python manage.py recalc_partner_tiers --apply            # только повышения
    python manage.py recalc_partner_tiers --apply --allow-downgrade
"""

from django.core.management.base import BaseCommand

from api.models import ClientProfile, client_turnover, grown_partner_status, partner_tier_for_total
from api.services.tiers import recalculate_all


class Command(BaseCommand):
    help = "Пересчитывает ранги клиентов по обороту (закупки + оплаченные заказы)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Записать изменения. Без флага команда только показывает, что изменится.",
        )
        parser.add_argument(
            "--allow-downgrade",
            action="store_true",
            help="Понижать ранг, если оборот больше не дотягивает до порога.",
        )
        parser.add_argument(
            "--distributor",
            type=int,
            help="Ограничить клиентами одного дистрибьютора (ID).",
        )

    def handle(self, *args, **options):
        clients = ClientProfile.objects.all().order_by("id")
        if options["distributor"]:
            clients = clients.filter(distributor_id=options["distributor"])

        allow_downgrade = options["allow_downgrade"]

        if not options["apply"]:
            self._preview(clients, allow_downgrade)
            return

        stats = recalculate_all(allow_downgrade=allow_downgrade, queryset=clients)
        for client, result in stats["changes"]:
            self.stdout.write(
                f"  {client.company_name}: {result['old']} → {result['new']} "
                f"(оборот {self._money(result['turnover'])})"
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Проверено: {stats['checked']}, повышено: {stats['upgraded']}, "
                f"понижено: {stats['downgraded']}"
            )
        )

    def _preview(self, clients, allow_downgrade):
        changes = []
        for client in clients:
            turnover = client_turnover(client)
            new = (
                partner_tier_for_total(turnover)
                if allow_downgrade
                else grown_partner_status(client.partner_status, turnover)
            )
            if new != client.partner_status:
                changes.append((client, client.partner_status, new, turnover))

        self.stdout.write(f"Клиентов проверено: {clients.count()}")
        self.stdout.write(f"  под изменение: {len(changes)}")
        for client, old, new, turnover in changes[:20]:
            self.stdout.write(f"  {client.company_name}: {old} → {new} (оборот {self._money(turnover)})")

        if not changes:
            self.stdout.write(self.style.SUCCESS("Все ранги актуальны."))
            return

        self.stdout.write("")
        self.stdout.write(
            self.style.WARNING("Пробный прогон: ничего не изменено. Повторите с флагом --apply.")
        )

    @staticmethod
    def _money(value):
        return f"{value:,.0f} ₽".replace(",", " ")
