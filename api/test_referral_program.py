"""Реферальная программа (п. 7 ТЗ).

Логика по ТЗ: клиент раздаёт ссылку или код → новый сервис регистрируется по
ней → система связывает его с пригласившим → подарок появляется только после
реальной покупки выше порога.

До этого код приглашения был захардкожен в приложении одной строкой на всех, а
регистрация вообще не принимала код — связать пришедшего с пригласившим было
нечем.
"""

from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

from .models import (
    AuthToken,
    ClientProfile,
    Distributor,
    Order,
    OrderItem,
    Product,
    Purchase,
    Referral,
    Region,
    generate_referral_code,
)


class ReferralCodeTests(TestCase):
    def setUp(self):
        self.distributor = Distributor.objects.create(
            name="Дист", inn="1112223334", phone="1", email="d@e.co"
        )
        self.region = Region.objects.create(code="77", name="Москва", distributor=self.distributor)

    def _client(self, username, inn, company="Компания"):
        user = User.objects.create_user(username=username, password="pw")
        return ClientProfile.objects.create(
            user=user, inn=inn, company_name=company, contact_name="Иван",
            phone=username, region=self.region, city="Москва", distributor=self.distributor,
        )

    def test_every_client_gets_a_code(self):
        first = self._client("+79001110001", "1000000001")
        second = self._client("+79001110002", "1000000002")

        self.assertTrue(first.referral_code)
        self.assertTrue(second.referral_code)
        self.assertNotEqual(first.referral_code, second.referral_code)

    def test_code_format_is_readable(self):
        client = self._client("+79001110001", "1000000001")
        self.assertTrue(client.referral_code.startswith("AT-"))
        self.assertEqual(len(client.referral_code), 9)

    def test_code_avoids_confusable_characters(self):
        # Код диктуют по телефону: 0/O и 1/I путаются.
        for _ in range(30):
            code = generate_referral_code()
            self.assertNotRegex(code[3:], r"[01OIl]")

    def test_code_survives_profile_edit(self):
        client = self._client("+79001110001", "1000000001")
        original = client.referral_code

        client.company_name = "Новое название"
        client.save()

        client.refresh_from_db()
        self.assertEqual(client.referral_code, original)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ReferralRegistrationTests(TestCase):
    """Регистрация по ссылке-приглашению."""

    def setUp(self):
        self.http = Client()
        self.distributor = Distributor.objects.create(
            name="Дист", inn="1112223334", phone="1", email="d@e.co"
        )
        self.region = Region.objects.create(code="77", name="Москва", distributor=self.distributor)

        inviter_user = User.objects.create_user(username="+79001110000", password="pw")
        self.inviter = ClientProfile.objects.create(
            user=inviter_user, inn="5556667778", company_name="ООО Пригласивший",
            contact_name="Иван", phone="+79001110000",
            region=self.region, city="Москва", distributor=self.distributor,
        )
        self.inviter_token = AuthToken.objects.create(key="inviter-token", user=inviter_user)

    def _register(self, **overrides):
        payload = {
            "username": "+79002223344",
            "password": "verysecret123",
            "inn": "7778889990",
            "region_id": str(self.region.id),
            "company_name": "ООО Приглашённый",
            "contact_name": "Пётр",
            "store_address": "Москва, Ленина 1",
        }
        payload.update(overrides)
        return self.http.post("/api/register/", data=payload, content_type="application/json")

    def test_registration_by_code_links_to_inviter(self):
        response = self._register(referralCode=self.inviter.referral_code)
        self.assertEqual(response.status_code, 201, response.content)

        referral = Referral.objects.get(inviter=self.inviter)
        self.assertEqual(referral.invitee_inn, "7778889990")
        self.assertEqual(referral.invitee_name, "ООО Приглашённый")
        self.assertTrue(referral.is_registered)

    def test_short_ref_parameter_also_works(self):
        # Ссылка вида /register?ref=CODE
        self._register(ref=self.inviter.referral_code)
        self.assertTrue(Referral.objects.filter(inviter=self.inviter).exists())

    def test_code_is_case_insensitive(self):
        self._register(referralCode=self.inviter.referral_code.lower())
        self.assertTrue(Referral.objects.filter(inviter=self.inviter).exists())

    def test_registration_without_code_creates_no_referral(self):
        response = self._register()
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Referral.objects.count(), 0)

    def test_unknown_code_does_not_break_registration(self):
        response = self._register(referralCode="AT-NOSUCH")

        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(ClientProfile.objects.filter(inn="7778889990").exists())
        self.assertEqual(Referral.objects.count(), 0)

    def test_registration_alone_earns_no_gift(self):
        """Подарок — за покупку, а не за пустую регистрацию (правило из ТЗ)."""
        self._register(referralCode=self.inviter.referral_code)

        referral = Referral.objects.get(inviter=self.inviter)
        self.assertTrue(referral.is_registered)
        self.assertFalse(referral.has_purchase)
        self.assertFalse(referral.condition_met)
        self.assertEqual(referral.gift, "")

    def test_cannot_invite_yourself(self):
        # Регистрация с собственным кодом: записи быть не должно.
        self._register(
            referralCode=self.inviter.referral_code,
            username=self.inviter.phone,
        )
        self.assertEqual(Referral.objects.count(), 0)

    def test_double_registration_does_not_duplicate_referral(self):
        self._register(referralCode=self.inviter.referral_code)
        # Повтор с тем же ИНН в том же регионе отбивается 409, дубля нет.
        self._register(referralCode=self.inviter.referral_code, username="+79005554433")
        self.assertEqual(Referral.objects.filter(inviter=self.inviter).count(), 1)


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    REFERRAL_BONUS_THRESHOLD=30000,
    REFERRAL_BONUS_GIFT="Сертификат на 5000 ₽",
)
class ReferralGiftTests(TestCase):
    """Оборот приглашённого учитывается только по подтверждённым закупкам.

    Плоский подарок «набрал 30 000 — сертификат на 5 000» убран: это было
    16,7% от оборота, выше маржи. Начисление процентом проверяется в
    test_referral_bonus_percent.py, здесь — только учёт оборота.
    """

    def setUp(self):
        self.http = Client()
        self.distributor = Distributor.objects.create(
            name="Дист", inn="1112223334", phone="1", email="d@e.co"
        )
        self.region = Region.objects.create(code="77", name="Москва", distributor=self.distributor)
        self.product = Product.objects.create(
            distributor=self.distributor, sku="P-1", name="Краска",
            category="Краски", price=Decimal(1000), quantity=1000,
        )

        inviter_user = User.objects.create_user(username="+79001110000", password="pw")
        self.inviter = ClientProfile.objects.create(
            user=inviter_user, inn="5556667778", company_name="Пригласивший",
            contact_name="Иван", phone="+79001110000",
            region=self.region, city="Москва", distributor=self.distributor,
        )
        self.inviter_token = AuthToken.objects.create(key="inviter-token", user=inviter_user)

        invitee_user = User.objects.create_user(username="+79002223344", password="pw")
        self.invitee = ClientProfile.objects.create(
            user=invitee_user, inn="7778889990", company_name="Приглашённый",
            contact_name="Пётр", phone="+79002223344",
            region=self.region, city="Москва", distributor=self.distributor,
        )
        self.referral = Referral.objects.create(
            inviter=self.inviter, invitee_inn=self.invitee.inn,
            invitee_name=self.invitee.company_name, region=self.region.name,
        )

    def _purchase(self, amount, status="verified"):
        return Purchase.objects.create(
            client=self.invitee, distributor=self.distributor,
            document_number=f"D{amount}", date="2026-01-01",
            total_amount=Decimal(amount), status=status,
        )

    def _fulfilled_order(self, amount):
        order = Order.objects.create(
            client=self.invitee, distributor=self.distributor, status="fulfilled",
        )
        OrderItem.objects.create(
            order=order, product=self.product, sku="P-1", name="Краска",
            price=Decimal(amount), quantity=1,
        )
        return order

    def test_any_verified_purchase_counts(self):
        """Порога больше нет: ставка ненулевая с первого рубля."""
        self._purchase(20_000)
        self.referral.sync_from_invitee()

        self.assertTrue(self.referral.has_purchase)
        self.assertTrue(self.referral.condition_met)

    def test_turnover_is_tracked(self):
        self._purchase(35_000)
        self.referral.sync_from_invitee()

        self.assertTrue(self.referral.condition_met)
        self.assertEqual(float(self.referral.purchase_amount), 35_000)

    def test_unverified_purchase_does_not_count(self):
        self._purchase(50_000, status="pending_verification")
        self.referral.sync_from_invitee()

        self.assertFalse(self.referral.has_purchase)
        self.assertFalse(self.referral.condition_met)

    def test_fulfilled_order_counts_too(self):
        self._fulfilled_order(40_000)
        self.referral.sync_from_invitee()

        self.assertTrue(self.referral.condition_met)

    @override_settings(REFERRAL_BONUS_THRESHOLD=10_000, REFERRAL_BONUS_GIFT="Отсрочка 14 дней")
    def test_turnover_counts_purchases_and_orders_together(self):
        self._purchase(12_000)
        self._fulfilled_order(8_000)
        self.referral.sync_from_invitee()

        self.assertEqual(float(self.referral.purchase_amount), 20_000)

    # ── Что видит пригласивший в приложении ──────────────────────────────────

    def _fetch(self):
        response = self.http.get(
            "/api/referrals/", HTTP_AUTHORIZATION=f"Bearer {self.inviter_token.key}"
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_endpoint_returns_own_code_and_link(self):
        data = self._fetch()

        self.assertEqual(data["referralCode"], self.inviter.referral_code)
        self.assertIn(f"ref={self.inviter.referral_code}", data["inviteLink"])
        # Ступени приходят с сервера — приложение их не зашивает.
        self.assertEqual(data["bonusTiers"][0], {"from": 0.0, "rate": 0.5})
        self.assertEqual(data["bonusTiers"][-1], {"from": 800000.0, "rate": 2.5})
        self.assertEqual(data["activityMin"], 10000.0)

    def test_endpoint_shows_purchase_made_after_invite(self):
        """Покупка случилась позже создания записи — цифры должны подтянуться."""
        self._purchase(35_000)

        data = self._fetch()
        item = data["results"][0]

        self.assertTrue(item["hasPurchase"])
        self.assertTrue(item["conditionMet"])
        self.assertEqual(item["purchaseAmount"], 35_000.0)
        self.assertEqual(data["stats"]["buyersCount"], 1)
        # 35 000 — первая ступень, 0,5% = 175 ₽.
        self.assertEqual(item["bonusRate"], 0.5)
        self.assertEqual(item["bonusEarned"], 175.0)

    def test_client_profile_exposes_referral_code(self):
        response = self.http.get(
            "/api/auth/me/", HTTP_AUTHORIZATION=f"Bearer {self.inviter_token.key}"
        )
        self.assertEqual(response.status_code, 200, response.content)

    # ── Профиль и главный экран ──────────────────────────────────────────────

    def _dashboard(self):
        response = self.http.get(
            "/api/dashboard/", HTTP_AUTHORIZATION=f"Bearer {self.inviter_token.key}"
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_dashboard_exposes_referral_code(self):
        """Код нужен в профиле — за ним не должно быть отдельного запроса."""
        data = self._dashboard()

        self.assertEqual(data["client"]["referralCode"], self.inviter.referral_code)

    def test_dashboard_counts_invited_services(self):
        data = self._dashboard()

        self.assertEqual(data["referralSummary"]["invitedCount"], 1)
        self.assertEqual(data["referralSummary"]["buyersCount"], 0)
        self.assertEqual(data["referralSummary"]["giftCount"], 0)

    def test_dashboard_counts_gift_as_soon_as_the_invitee_buys(self):
        """Согласования больше нет: покупка приглашённого сразу даёт бонус."""
        self._purchase(35_000)
        self.referral.sync_from_invitee()

        data = self._dashboard()
        self.assertEqual(data["referralSummary"]["buyersCount"], 1)
        self.assertEqual(data["referralSummary"]["giftCount"], 1)

    def test_dashboard_of_client_without_invites_shows_zero(self):
        """У приглашённого своих рефералов нет — цифры не должны «протекать»."""
        token = AuthToken.objects.create(key="invitee-token", user=self.invitee.user)

        response = self.http.get("/api/dashboard/", HTTP_AUTHORIZATION=f"Bearer {token.key}")
        summary = response.json()["referralSummary"]

        self.assertEqual(summary["invitedCount"], 0)
        self.assertEqual(summary["giftCount"], 0)
