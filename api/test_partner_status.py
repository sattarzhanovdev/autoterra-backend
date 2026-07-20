from decimal import Decimal

from django.test import TestCase
from django.contrib.auth.models import User
from django.db.models import Sum

from .models import (
    ClientProfile,
    Distributor,
    Region,
    Purchase,
    partner_tier_for_total,
    grown_partner_status,
)


class PartnerTierLogicTests(TestCase):
    def test_tier_thresholds(self):
        self.assertEqual(partner_tier_for_total(Decimal(0)), "Silver")
        self.assertEqual(partner_tier_for_total(Decimal(499_999)), "Silver")
        self.assertEqual(partner_tier_for_total(Decimal(500_000)), "Gold")
        self.assertEqual(partner_tier_for_total(Decimal(2_000_000)), "Platinum")
        self.assertEqual(partner_tier_for_total(Decimal(5_000_000)), "Certified Partner")

    def test_grow_only_never_downgrades(self):
        # Grows when earned tier is higher.
        self.assertEqual(grown_partner_status("Silver", Decimal(600_000)), "Gold")
        # Keeps the higher current status even if total would map lower.
        self.assertEqual(grown_partner_status("Platinum", Decimal(0)), "Platinum")
        # Unknown/blank current treated as Silver.
        self.assertEqual(grown_partner_status("", Decimal(500_000)), "Gold")


class PartnerStatusOnVerifyTests(TestCase):
    def setUp(self):
        self.dist_user = User.objects.create_user(username="dist", password="p")
        self.distributor = Distributor.objects.create(
            user=self.dist_user, name="Dist", inn="1", phone="1", email="d@e.co"
        )
        self.region = Region.objects.create(code="77", name="Msk", distributor=self.distributor)
        self.client_user = User.objects.create_user(username="client")
        self.profile = ClientProfile.objects.create(
            user=self.client_user,
            inn="1234567890",
            company_name="Client",
            region=self.region,
            distributor=self.distributor,
            phone="1", city="Msk", contact_name="Me",
        )

    def _recompute(self):
        """Mirror the recompute the verify endpoint performs."""
        verified_total = (
            Purchase.objects.filter(client=self.profile, status="verified")
            .aggregate(total=Sum("total_amount"))["total"]
            or 0
        )
        new_status = grown_partner_status(self.profile.partner_status, verified_total)
        ClientProfile.objects.filter(pk=self.profile.pk).update(
            total_purchases=verified_total, partner_status=new_status
        )
        self.profile.refresh_from_db()

    def test_starts_silver(self):
        self.assertEqual(self.profile.partner_status, "Silver")

    def test_grows_to_gold_after_enough_verified_purchases(self):
        Purchase.objects.create(
            client=self.profile, distributor=self.distributor,
            document_number="A1", date="2026-01-01", total_amount=Decimal(600_000),
            status="verified",
        )
        self._recompute()
        self.assertEqual(self.profile.partner_status, "Gold")
        self.assertEqual(self.profile.total_purchases, Decimal(600_000))

    def test_unverified_purchases_do_not_count(self):
        Purchase.objects.create(
            client=self.profile, distributor=self.distributor,
            document_number="A2", date="2026-01-01", total_amount=Decimal(600_000),
            status="pending",
        )
        self._recompute()
        self.assertEqual(self.profile.partner_status, "Silver")
