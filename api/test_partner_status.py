"""Ранг растёт через реальный эндпоинт подтверждения закупки.

Логику лестницы и оборота проверяет test_partner_tiers; здесь — что путь
«дистрибьютор подтвердил закупку → у клиента вырос ранг» действительно
отрабатывает, вместе с кэшем total_purchases.
"""

from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase

from .models import AuthToken, ClientProfile, Distributor, PartnerTier, Purchase, Region


class PartnerStatusOnVerifyTests(TestCase):
    def setUp(self):
        self.http = Client()
        self.dist_user = User.objects.create_user(username="dist", password="p")
        self.dist_user.profile.role = "distributor"
        self.dist_user.profile.save()
        self.distributor = Distributor.objects.create(
            user=self.dist_user, name="Dist", inn="1112223334", phone="1", email="d@e.co"
        )
        self.dist_token = AuthToken.objects.create(key="dist-token", user=self.dist_user)
        self.region = Region.objects.create(code="77", name="Msk", distributor=self.distributor)

        self.client_user = User.objects.create_user(username="client")
        self.profile = ClientProfile.objects.create(
            user=self.client_user, inn="1234567890", company_name="Client",
            region=self.region, distributor=self.distributor,
            phone="1", city="Msk", contact_name="Me",
        )

    def _purchase(self, amount, status="pending_verification"):
        return Purchase.objects.create(
            client=self.profile, distributor=self.distributor,
            document_number=f"A{amount}", date="2026-01-01",
            total_amount=Decimal(amount), status=status,
        )

    def _verify(self, purchase, status="verified"):
        return self.http.post(
            f"/api/distributor/purchases/{purchase.id}/verify/",
            data={"status": status},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.dist_token.key}",
        )

    def test_starts_at_base_tier(self):
        self.assertEqual(self.profile.partner_status, "Базовый")

    def test_verifying_purchase_grows_the_tier(self):
        purchase = self._purchase(600_000)

        response = self._verify(purchase)
        self.assertEqual(response.status_code, 200, response.content)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.partner_status, "Silver")
        self.assertEqual(self.profile.total_purchases, Decimal(600_000))

    def test_rejected_purchase_does_not_count(self):
        purchase = self._purchase(600_000)

        self._verify(purchase, status="rejected")

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.partner_status, "Базовый")
        self.assertEqual(self.profile.total_purchases, Decimal(0))

    def test_purchases_accumulate_across_documents(self):
        self._verify(self._purchase(1_200_000))
        self._verify(self._purchase(1_000_000))

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.total_purchases, Decimal(2_200_000))
        self.assertEqual(self.profile.partner_status, "Gold")

    def test_new_thresholds_apply_to_the_next_verification(self):
        PartnerTier.objects.filter(name="Silver").update(threshold=100_000)

        self._verify(self._purchase(150_000))

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.partner_status, "Silver")
