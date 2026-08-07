"""Реферальный бонус процентом от закупок приглашённого.

Прежняя схема платила 5 000 ₽ за оборот 30 000 ₽ — 16,7%, заметно выше маржи.
Теперь ставка растёт по ступеням и применяется ко всему обороту приглашённого.

Это деньги, поэтому проверяем не только счастливый путь: границы ступеней,
повторные вызовы, переход на ступень выше и сгорание.
"""

from datetime import date
from decimal import Decimal
from io import StringIO

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from .models import BonusTransaction, ClientProfile, Distributor, Purchase, Referral, Region
from .services import referral_bonus as rb
from .services.bonuses import balance


class RateTests(TestCase):
    def test_tier_boundaries(self):
        """Ставка меняется ровно на пороге, не раньше."""
        cases = {
            0: "0.5",
            1: "0.5",
            99_999: "0.5",
            100_000: "1.0",
            199_999: "1.0",
            200_000: "1.5",
            399_999: "1.5",
            400_000: "2.0",
            799_999: "2.0",
            800_000: "2.5",
            5_000_000: "2.5",
        }
        for volume, expected in cases.items():
            self.assertEqual(rb.rate_for(volume), Decimal(expected), f"оборот {volume}")

    def test_earned_matches_the_agreed_example(self):
        """Пример из согласования: закуп 300 000 → 1,5% → 4 500 ₽."""
        self.assertEqual(rb.earned_for(300_000), Decimal("4500.00"))

    def test_earned_across_tiers(self):
        self.assertEqual(rb.earned_for(0), Decimal("0.00"))
        self.assertEqual(rb.earned_for(50_000), Decimal("250.00"))
        self.assertEqual(rb.earned_for(100_000), Decimal("1000.00"))
        self.assertEqual(rb.earned_for(200_000), Decimal("3000.00"))
        self.assertEqual(rb.earned_for(400_000), Decimal("8000.00"))
        self.assertEqual(rb.earned_for(800_000), Decimal("20000.00"))

    def test_next_tier(self):
        remaining, rate = rb.next_tier(70_000)
        self.assertEqual(remaining, Decimal(30_000))
        self.assertEqual(rate, Decimal("1.0"))
        # На верхней ступени расти уже некуда.
        self.assertIsNone(rb.next_tier(900_000))

    @override_settings(REFERRAL_BONUS_TIERS=[(0, "1"), (50_000, "3")])
    def test_tiers_are_configurable(self):
        self.assertEqual(rb.rate_for(10_000), Decimal("1"))
        self.assertEqual(rb.earned_for(50_000), Decimal("1500.00"))


class _Base(TestCase):
    def setUp(self):
        self.distributor = Distributor.objects.create(
            name="Дист", inn="1112223334", phone="1", email="d@e.co"
        )
        self.region = Region.objects.create(
            code="59", name="Пермь - Урал", distributor=self.distributor
        )
        self.inviter = self._client("+79001110001", "7707083893", "Пригласивший")
        self.invitee = self._client("+79001110002", "7736050003", "Приглашённый")

    def _client(self, username, inn, company="Компания"):
        user = User.objects.create_user(username=username, password="pw")
        return ClientProfile.objects.create(
            user=user, inn=inn, company_name=company, contact_name="Иван",
            phone=username, region=self.region, city="Пермь",
            distributor=self.distributor, status="active",
        )

    def _referral(self, confirmation="confirmed"):
        return Referral.objects.create(
            inviter=self.inviter,
            invitee_inn=self.invitee.inn,
            invitee_name=self.invitee.company_name,
            region="Пермь - Урал",
            confirmation=confirmation,
        )

    def _purchase(self, client, amount, status="verified", when=None):
        return Purchase.objects.create(
            client=client,
            distributor=self.distributor,
            document_number=f"DOC-{amount}",
            date=when or date(2026, 8, 1),
            total_amount=Decimal(amount),
            status=status,
        )


class AccrualTests(_Base):
    def test_accrues_percentage_of_invitee_turnover(self):
        self._referral()
        self._purchase(self.invitee, 300_000)

        self.assertEqual(balance(self.inviter), Decimal("4500.00"))

    def test_repeat_call_accrues_nothing(self):
        referral = self._referral()
        self._purchase(self.invitee, 300_000)

        # Закупок не прибавилось — доначислять нечего.
        self.assertEqual(rb.accrue(referral), Decimal("0.00"))
        self.assertEqual(balance(self.inviter), Decimal("4500.00"))

    def test_tier_upgrade_tops_up_retroactively(self):
        """Ставка растёт на весь оборот, доначисляем только разницу."""
        self._referral()
        self._purchase(self.invitee, 100_000)
        self.assertEqual(balance(self.inviter), Decimal("1000.00"))  # 1% от 100k

        # Ещё 100 000 — ступень 200 000, ставка 1,5% на весь оборот = 3 000 ₽.
        # Доначислить должно ровно 2 000, а не начислить 3 000 поверх старого.
        self._purchase(self.invitee, 100_000)

        self.assertEqual(balance(self.inviter), Decimal("3000.00"))

    def test_accrue_computes_delta_when_called_directly(self):
        """Связку могли завести уже после закупок — тогда считаем всё разом."""
        self._purchase(self.invitee, 300_000)
        referral = self._referral()
        referral.sync_from_invitee()

        self.assertEqual(rb.accrue(referral), Decimal("4500.00"))
        self.assertEqual(rb.accrue(referral), Decimal("0.00"))

    def test_unconfirmed_claim_pays_nothing(self):
        """Пока приглашённый не подтвердил, что привели именно вы."""
        referral = self._referral(confirmation="pending")
        self._purchase(self.invitee, 300_000)

        self.assertEqual(rb.accrue(referral), Decimal("0.00"))
        self.assertEqual(balance(self.inviter), Decimal("0.00"))

    def test_unverified_purchase_does_not_count(self):
        self._referral()
        self._purchase(self.invitee, 300_000, status="pending_verification")

        self.assertEqual(balance(self.inviter), Decimal("0.00"))

    def test_accrues_automatically_when_purchase_is_verified(self):
        """Начисление по факту оплаты — сигналом, а не отдельным вызовом."""
        self._referral()
        purchase = self._purchase(self.invitee, 300_000, status="pending_verification")
        self.assertEqual(balance(self.inviter), Decimal("0.00"))

        purchase.status = "verified"
        purchase.save()

        self.assertEqual(balance(self.inviter), Decimal("4500.00"))

    def test_already_verified_purchase_resaved_accrues_nothing(self):
        self._referral()
        purchase = self._purchase(self.invitee, 300_000)
        self.assertEqual(balance(self.inviter), Decimal("4500.00"))

        purchase.rejection_reason = "правка мимо статуса"
        purchase.save()

        self.assertEqual(balance(self.inviter), Decimal("4500.00"))


class ExpiryTests(_Base):
    def _earn(self, amount=300_000):
        self._referral()
        self._purchase(self.invitee, amount)

    def test_balance_burns_when_inviter_is_inactive(self):
        self._earn()
        self.assertEqual(balance(self.inviter), Decimal("4500.00"))

        burnt = rb.expire_if_inactive(self.inviter, 2026, 8)

        self.assertEqual(burnt, Decimal("4500.00"))
        self.assertEqual(balance(self.inviter), Decimal("0.00"))

    def test_active_inviter_keeps_bonuses(self):
        self._earn()
        self._purchase(self.inviter, 10_000, when=date(2026, 8, 15))

        self.assertEqual(rb.expire_if_inactive(self.inviter, 2026, 8), Decimal("0.00"))
        self.assertEqual(balance(self.inviter), Decimal("4500.00"))

    def test_below_minimum_still_burns(self):
        self._earn()
        self._purchase(self.inviter, 9_999, when=date(2026, 8, 15))

        self.assertEqual(rb.expire_if_inactive(self.inviter, 2026, 8), Decimal("4500.00"))

    def test_expiry_is_idempotent(self):
        """Повторный запуск команды за тот же месяц не уводит баланс в минус."""
        self._earn()
        rb.expire_if_inactive(self.inviter, 2026, 8)

        self.assertEqual(rb.expire_if_inactive(self.inviter, 2026, 8), Decimal("0.00"))
        self.assertEqual(balance(self.inviter), Decimal("0.00"))

    def test_burnt_bonus_is_not_accrued_again(self):
        """Иначе следующая закупка приглашённого вернула бы сгоревшее."""
        self._referral()
        self._purchase(self.invitee, 100_000)
        rb.expire_if_inactive(self.inviter, 2026, 8)
        self.assertEqual(balance(self.inviter), Decimal("0.00"))

        # Приглашённый закупает ещё — начисляем только прирост, не всё заново.
        self._purchase(self.invitee, 100_000)

        self.assertEqual(balance(self.inviter), Decimal("2000.00"))

    def test_expiry_leaves_a_trace_in_the_ledger(self):
        self._earn()
        rb.expire_if_inactive(self.inviter, 2026, 8)

        entry = BonusTransaction.objects.get(client=self.inviter, kind="expired")
        self.assertEqual(entry.amount, Decimal("-4500.00"))
        self.assertIn("08.2026", entry.comment)


class BackfillCommandTests(_Base):
    """Закупки, подтверждённые до выката, сигналом уже не поймать."""

    def test_recalc_credits_existing_turnover(self):
        from django.core.management import call_command

        # Связка появляется после закупки — сигнал по ней не сработал.
        self._purchase(self.invitee, 300_000)
        self._referral()
        self.assertEqual(balance(self.inviter), Decimal("0.00"))

        call_command("recalc_referral_bonuses", "--apply", stdout=StringIO())

        self.assertEqual(balance(self.inviter), Decimal("4500.00"))

    def test_recalc_is_idempotent(self):
        from django.core.management import call_command

        self._purchase(self.invitee, 300_000)
        self._referral()
        call_command("recalc_referral_bonuses", "--apply", stdout=StringIO())
        call_command("recalc_referral_bonuses", "--apply", stdout=StringIO())

        self.assertEqual(balance(self.inviter), Decimal("4500.00"))

    def test_dry_run_credits_nothing(self):
        from django.core.management import call_command

        self._purchase(self.invitee, 300_000)
        self._referral()

        call_command("recalc_referral_bonuses", stdout=StringIO())

        self.assertEqual(balance(self.inviter), Decimal("0.00"))

    def test_unconfirmed_claim_is_skipped(self):
        from django.core.management import call_command

        self._purchase(self.invitee, 300_000)
        self._referral(confirmation="pending")

        call_command("recalc_referral_bonuses", "--apply", stdout=StringIO())

        self.assertEqual(balance(self.inviter), Decimal("0.00"))
