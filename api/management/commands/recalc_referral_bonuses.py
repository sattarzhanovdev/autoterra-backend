"""Досчитывает реферальные бонусы по уже накопленным данным.

Начисление работает на сигналах: закупка перешла в «подтверждена» — бонус
начислился. Но закупки, подтверждённые до перехода на процентную схему, статус
больше не меняют, и сами по себе ничего не начислят.

Эта команда проходит по всем связкам, пересчитывает оборот приглашённых и
доначисляет разницу. Запускать один раз после выката, дальше всё идёт
сигналами. Повторный запуск безопасен: accrue() начисляет только нехватку до
текущей ступени, поэтому второй прогон даст нули.

Использование:
    python manage.py recalc_referral_bonuses            # что доначислится
    python manage.py recalc_referral_bonuses --apply    # доначислить
"""

from django.core.management.base import BaseCommand

from api.models import Referral
from api.services import referral_bonus as rb


class Command(BaseCommand):
    help = "Досчитывает реферальные бонусы по уже подтверждённым закупкам"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Записать начисления. Без этого флага команда только показывает найденное.",
        )
        parser.add_argument(
            "--drop-legacy-gifts",
            action="store_true",
            help="Убрать плоские подарки старой схемы («Сертификат на 5000 ₽»). "
                 "Они остались в карточках от порогового бонуса, которого больше нет.",
        )

    def handle(self, *args, **options):
        referrals = Referral.objects.select_related("inviter").order_by("id")
        total_referrals = referrals.count()
        self.stdout.write(f"Связок всего: {total_referrals}")

        if not total_referrals:
            self.stdout.write(self.style.SUCCESS("Пересчитывать нечего."))
            return

        planned = []
        skipped_unconfirmed = 0

        for referral in referrals:
            referral.sync_from_invitee()
            if not referral.counts_toward_bonus:
                skipped_unconfirmed += 1
                continue

            volume = referral.purchase_amount
            should_have = rb.earned_for(volume)
            already = rb.accrued_for(referral)
            delta = should_have - already
            if delta > 0:
                planned.append((referral, volume, should_have, already, delta))

        if skipped_unconfirmed:
            self.stdout.write(
                f"  не подтверждены приглашённым, пропущены: {skipped_unconfirmed}"
            )
        self.stdout.write(f"  под доначисление: {len(planned)}")
        self.stdout.write("")

        for referral, volume, should_have, already, delta in planned:
            self.stdout.write(
                f"  #{referral.id} {referral.inviter.company_name} ← «{referral.invitee_name}»: "
                f"оборот {volume} ₽ · ставка {rb.rate_for(volume)}% · "
                f"положено {should_have} ₽, начислено {already} ₽ → +{delta} ₽"
            )

        if not planned:
            self.stdout.write(self.style.SUCCESS("Всё уже начислено."))
            return

        if not options["apply"]:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING("Пробный прогон: ничего не изменено. Повторите с флагом --apply.")
            )
            return

        credited = sum((rb.accrue(referral) for referral, *_ in planned), start=0)
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(f"Доначислено: {credited} ₽ по {len(planned)} связкам")
        )

        if options["drop_legacy_gifts"]:
            self._drop_legacy_gifts()

    def _drop_legacy_gifts(self):
        """Стирает подарки, выставленные автоматически по старому порогу.

        Начисление денег они не делали — деньги теперь идут процентом, — но в
        карточке приглашения так и висит «Сертификат на 5000 ₽» рядом с
        реальной ставкой. Подарки, по которым бонус уже начислен, не трогаем:
        за ними стоят настоящие операции по счёту.
        """
        from api.models import BonusTransaction

        paid_out = set(
            BonusTransaction.objects.filter(kind="referral", referral__isnull=False)
            .exclude(comment__contains="%")
            .values_list("referral_id", flat=True)
        )
        stale = Referral.objects.exclude(gift="").exclude(id__in=paid_out)
        count = stale.count()
        if not count:
            self.stdout.write("Плоских подарков не осталось.")
            return

        stale.update(gift="", gift_amount=0, gift_status="none", gift_comment="")
        self.stdout.write(self.style.SUCCESS(f"Убрано плоских подарков: {count}"))
