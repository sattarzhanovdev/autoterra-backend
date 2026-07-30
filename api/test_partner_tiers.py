"""Ранги клиентов: пороги настраиваются в админке, ранг растёт по обороту."""

from decimal import Decimal
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from .models import (
    ClientProfile,
    Distributor,
    Order,
    OrderItem,
    PartnerTier,
    Product,
    Purchase,
    Region,
    client_turnover,
    grown_partner_status,
    partner_tier_for_total,
)
from .services.tiers import recalculate_all, sync_client_tier


class PartnerTierLadderTests(TestCase):
    """Лестница рангов читается из базы, а не из хардкода."""

    def test_default_ladder_seeded_by_migration(self):
        names = list(PartnerTier.objects.order_by("threshold").values_list("name", flat=True))
        self.assertEqual(names, ["Базовый", "Silver", "Gold", "Platinum"])

    def test_tier_for_turnover(self):
        self.assertEqual(partner_tier_for_total(0), "Базовый")
        self.assertEqual(partner_tier_for_total(499_999), "Базовый")
        self.assertEqual(partner_tier_for_total(500_000), "Silver")
        self.assertEqual(partner_tier_for_total(2_000_000), "Gold")
        self.assertEqual(partner_tier_for_total(5_000_000), "Platinum")
        self.assertEqual(partner_tier_for_total(50_000_000), "Platinum")

    def test_admin_can_change_thresholds(self):
        PartnerTier.objects.filter(name="Silver").update(threshold=100_000)
        self.assertEqual(partner_tier_for_total(150_000), "Silver")

    def test_admin_can_add_a_tier(self):
        PartnerTier.objects.create(name="Diamond", threshold=10_000_000)
        self.assertEqual(partner_tier_for_total(12_000_000), "Diamond")

    def test_disabled_tier_is_skipped(self):
        PartnerTier.objects.filter(name="Gold").update(is_active=False)
        self.assertEqual(partner_tier_for_total(2_500_000), "Silver")

    def test_empty_table_falls_back_to_defaults(self):
        PartnerTier.objects.all().delete()
        self.assertEqual(partner_tier_for_total(600_000), "Silver")

    def test_growth_never_downgrades(self):
        self.assertEqual(grown_partner_status("Базовый", 600_000), "Silver")
        self.assertEqual(grown_partner_status("Platinum", 0), "Platinum")
        self.assertEqual(grown_partner_status("", 500_000), "Silver")

    def test_unknown_status_replaced_by_earned(self):
        # «Certified Partner» больше не в лестнице — клиент получает
        # заслуженный ранг, а не остаётся с висящим статусом.
        self.assertEqual(grown_partner_status("Certified Partner", 600_000), "Silver")


class TurnoverTests(TestCase):
    def setUp(self):
        self.distributor = Distributor.objects.create(
            name="Dist", inn="1112223334", phone="1", email="d@e.co"
        )
        self.region = Region.objects.create(code="77", name="Msk", distributor=self.distributor)
        user = User.objects.create_user(username="+79001110000", password="pw")
        self.client_profile = ClientProfile.objects.create(
            user=user, inn="5556667778", company_name="Автосервис",
            contact_name="Иван", phone="+79001110000",
            region=self.region, city="Москва", distributor=self.distributor,
        )
        self.product = Product.objects.create(
            distributor=self.distributor, sku="P-1", name="Краска",
            category="Краски", price=Decimal(1000), quantity=100,
        )

    def _purchase(self, amount, status="verified"):
        return Purchase.objects.create(
            client=self.client_profile, distributor=self.distributor,
            document_number=f"D{amount}", date="2026-01-01",
            total_amount=Decimal(amount), status=status,
        )

    def _order(self, amount, status="paid"):
        """Сумма заказа считается по позициям — своего поля у него нет."""
        order = Order.objects.create(
            client=self.client_profile, distributor=self.distributor, status=status,
        )
        OrderItem.objects.create(
            order=order, product=self.product, sku=self.product.sku,
            name=self.product.name, price=Decimal(amount), quantity=1,
        )
        return order

    def test_turnover_counts_verified_purchases(self):
        self._purchase(300_000)
        self.assertEqual(client_turnover(self.client_profile), Decimal(300_000))

    def test_turnover_ignores_unverified_purchases(self):
        self._purchase(300_000, status="pending_verification")
        self.assertEqual(client_turnover(self.client_profile), Decimal(0))

    def test_turnover_counts_paid_orders(self):
        self._order(200_000)
        self.assertEqual(client_turnover(self.client_profile), Decimal(200_000))

    def test_turnover_ignores_unpaid_orders(self):
        self._order(200_000, status="new")
        self._order(150_000, status="confirmed")
        self.assertEqual(client_turnover(self.client_profile), Decimal(0))

    def test_turnover_sums_both_sources(self):
        self._purchase(300_000)
        self._order(250_000)
        self.assertEqual(client_turnover(self.client_profile), Decimal(550_000))

    def test_orders_alone_can_raise_the_rank(self):
        self._order(600_000)
        sync_client_tier(self.client_profile)
        self.assertEqual(self.client_profile.partner_status, "Silver")


class TierSyncTests(TestCase):
    def setUp(self):
        self.distributor = Distributor.objects.create(
            name="Dist", inn="1112223334", phone="1", email="d@e.co"
        )
        self.region = Region.objects.create(code="77", name="Msk", distributor=self.distributor)

    def _client(self, username, inn, status="Базовый"):
        user = User.objects.create_user(username=username, password="pw")
        return ClientProfile.objects.create(
            user=user, inn=inn, company_name=f"Компания {username}",
            partner_status=status, contact_name="Иван", phone="+79001110000",
            region=self.region, city="Москва", distributor=self.distributor,
        )

    def _purchase(self, client, amount):
        Purchase.objects.create(
            client=client, distributor=self.distributor,
            document_number=f"D{client.id}-{amount}", date="2026-01-01",
            total_amount=Decimal(amount), status="verified",
        )

    def test_new_client_starts_at_base(self):
        client = self._client("c1", "1000000001")
        sync_client_tier(client)
        self.assertEqual(client.partner_status, "Базовый")

    def test_sync_promotes_and_caches_turnover(self):
        client = self._client("c1", "1000000001")
        self._purchase(client, 2_500_000)

        result = sync_client_tier(client)

        self.assertEqual(result["old"], "Базовый")
        self.assertEqual(result["new"], "Gold")
        self.assertTrue(result["changed"])
        client.refresh_from_db()
        self.assertEqual(client.partner_status, "Gold")
        self.assertEqual(client.total_purchases, Decimal(2_500_000))

    def test_sync_does_not_downgrade_by_default(self):
        client = self._client("c1", "1000000001", status="Platinum")
        sync_client_tier(client)
        self.assertEqual(client.partner_status, "Platinum")

    def test_downgrade_when_explicitly_allowed(self):
        client = self._client("c1", "1000000001", status="Platinum")
        sync_client_tier(client, allow_downgrade=True)
        self.assertEqual(client.partner_status, "Базовый")

    def test_recalculate_all_reports_counts(self):
        first = self._client("c1", "1000000001")
        self._purchase(first, 600_000)
        self._client("c2", "1000000002")

        stats = recalculate_all()

        self.assertEqual(stats["checked"], 2)
        self.assertEqual(stats["upgraded"], 1)
        self.assertEqual(stats["downgraded"], 0)

    def test_threshold_change_takes_effect_on_recalc(self):
        client = self._client("c1", "1000000001")
        self._purchase(client, 150_000)
        sync_client_tier(client)
        self.assertEqual(client.partner_status, "Базовый")

        PartnerTier.objects.filter(name="Silver").update(threshold=100_000)
        sync_client_tier(client)

        client.refresh_from_db()
        self.assertEqual(client.partner_status, "Silver")


class RecalcCommandTests(TestCase):
    def setUp(self):
        self.distributor = Distributor.objects.create(
            name="Dist", inn="1112223334", phone="1", email="d@e.co"
        )
        self.region = Region.objects.create(code="77", name="Msk", distributor=self.distributor)
        user = User.objects.create_user(username="+79001110000", password="pw")
        self.client_profile = ClientProfile.objects.create(
            user=user, inn="5556667778", company_name="Автосервис",
            contact_name="Иван", phone="+79001110000",
            region=self.region, city="Москва", distributor=self.distributor,
        )
        Purchase.objects.create(
            client=self.client_profile, distributor=self.distributor,
            document_number="D1", date="2026-01-01",
            total_amount=Decimal(600_000), status="verified",
        )

    def _run(self, *args):
        out = StringIO()
        call_command("recalc_partner_tiers", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_changes_nothing(self):
        output = self._run()
        self.assertIn("Пробный прогон", output)
        self.assertIn("Базовый → Silver", output)
        self.client_profile.refresh_from_db()
        self.assertEqual(self.client_profile.partner_status, "Базовый")

    def test_apply_promotes(self):
        ClientProfile.objects.filter(pk=self.client_profile.pk).update(partner_status="Базовый")
        self._run("--apply")
        self.client_profile.refresh_from_db()
        self.assertEqual(self.client_profile.partner_status, "Silver")

    def test_downgrade_requires_flag(self):
        ClientProfile.objects.filter(pk=self.client_profile.pk).update(partner_status="Platinum")

        self._run("--apply")
        self.client_profile.refresh_from_db()
        self.assertEqual(self.client_profile.partner_status, "Platinum")

        self._run("--apply", "--allow-downgrade")
        self.client_profile.refresh_from_db()
        self.assertEqual(self.client_profile.partner_status, "Silver")
