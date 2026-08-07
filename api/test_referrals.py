from django.test import TestCase
from django.contrib.auth.models import User
from .models import Referral, ClientProfile, Region, Distributor, Purchase, Order

class ReferralAntiFraudTests(TestCase):
    def setUp(self):
        # 1. Setup Inviter and Environment
        self.distributor = Distributor.objects.create(name="Dist", inn="1", phone="1", email="d@e.co")
        self.region = Region.objects.create(code="77", name="Msk", distributor=self.distributor)
        
        self.inviter_user = User.objects.create_user(username="inviter")
        self.inviter = ClientProfile.objects.create(
            user=self.inviter_user, inn="1111111111", company_name="Inviter",
            region=self.region, distributor=self.distributor,
            city="Moscow", contact_name="Boss", phone="+79000000001"
        )

        # 2. Create Referral Record (the prospect)
        self.invitee_inn = "2222222222"
        self.referral = Referral.objects.create(
            inviter=self.inviter,
            invitee_inn=self.invitee_inn,
            invitee_name="Prospect Clinic",
            region="77"
        )

    def test_no_bonus_on_registration_only(self):
        """Проверка: регистрация реферала без покупок НЕ дает бонуса"""
        # Создаем профиль приглашенного (регистрация)
        invitee_user = User.objects.create_user(username="invitee")
        ClientProfile.objects.create(
            user=invitee_user, inn=self.invitee_inn, company_name="Prospect",
            region=self.region, distributor=self.distributor,
            city="Moscow", contact_name="Client", phone="+79000000002"
        )
        
        # Синхронизируем
        self.referral.sync_from_invitee()
        
        self.assertTrue(self.referral.is_registered)
        self.assertFalse(self.referral.has_purchase)
        self.assertFalse(self.referral.condition_met)
        self.assertEqual(self.referral.gift, "")

    def test_no_bonus_on_unverified_purchase(self):
        """Проверка: покупка в статусе 'new' (не проверена) НЕ дает бонуса"""
        invitee_user = User.objects.create_user(username="invitee2")
        invitee = ClientProfile.objects.create(
            user=invitee_user, inn=self.invitee_inn, company_name="Prospect",
            region=self.region, distributor=self.distributor,
            city="Moscow", contact_name="Client", phone="+79000000003"
        )
        
        # Загружаем крупную покупку, но она еще не проверена
        Purchase.objects.create(
            client=invitee, distributor=self.distributor,
            total_amount=50000, status="new", date="2026-06-06"
        )
        
        self.referral.sync_from_invitee()
        
        # is_registered=True, но has_purchase=False т.к. фильтр по 'verified'
        self.assertFalse(self.referral.condition_met)

    def test_turnover_accumulates_across_verified_purchases(self):
        """Оборот приглашённого суммируется по всем подтверждённым закупкам.

        Порога больше нет: бонус — процент от оборота, и ставка ненулевая с
        первого рубля. Сам расчёт процента — в test_referral_bonus_percent.
        """
        invitee_user = User.objects.create_user(username="invitee3")
        invitee = ClientProfile.objects.create(
            user=invitee_user, inn=self.invitee_inn, company_name="Prospect",
            region=self.region, distributor=self.distributor,
            city="Moscow", contact_name="Client", phone="+79000000004"
        )
        
        # 1. Покупка на 10 000 (verified)
        Purchase.objects.create(
            client=invitee, distributor=self.distributor,
            total_amount=10000, status="verified", date="2026-06-06"
        )
        self.referral.sync_from_invitee()
        self.assertEqual(float(self.referral.purchase_amount), 10000)
        self.assertTrue(self.referral.condition_met)

        # 2. Добавляем еще 25 000 (verified)
        Purchase.objects.create(
            client=invitee, distributor=self.distributor,
            total_amount=25000, status="verified", date="2026-06-07"
        )
        self.referral.sync_from_invitee()

        self.referral.refresh_from_db()
        self.assertEqual(float(self.referral.purchase_amount), 35000)
