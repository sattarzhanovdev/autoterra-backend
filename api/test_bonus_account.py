"""Бонусный счёт: начисление за реферала и списание в счёт заказа.

Подарок после согласования дистрибьютором превращается в рубли на счёте, а при
оплате уменьшает сумму, которая уходит в ЮKassa. Это деньги, поэтому проверяем
не только счастливый путь, но и повторы, отмены и попытки списать лишнее.
"""

from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

from .models import (
    AuthToken,
    BonusTransaction,
    ClientProfile,
    Distributor,
    Order,
    OrderItem,
    Product,
    Referral,
    Region,
)
from .services import bonuses


@override_settings(
    REFERRAL_BONUS_THRESHOLD=30000,
    REFERRAL_BONUS_GIFT="Сертификат на 5000 ₽",
    REFERRAL_BONUS_AMOUNT=Decimal("5000"),
)
class _Base(TestCase):
    def setUp(self):
        self.http = Client()
        self.distributor = Distributor.objects.create(
            name="Дист", inn="1112223334", phone="1", email="d@e.co"
        )
        self.region = Region.objects.create(
            code="77", name="Москва", distributor=self.distributor
        )
        self.product = Product.objects.create(
            distributor=self.distributor, sku="P-1", name="Краска",
            category="Краски", price=Decimal(1000), quantity=1000,
        )
        self.client_profile = self._client("+79001110000", "5556667778")
        self.token = AuthToken.objects.create(
            key="client-token", user=self.client_profile.user
        ).key

    def _client(self, username, inn, company="Компания"):
        user = User.objects.create_user(username=username, password="pw")
        return ClientProfile.objects.create(
            user=user, inn=inn, company_name=company, contact_name="Иван",
            phone=username, region=self.region, city="Москва",
            distributor=self.distributor, status="active",
        )

    def _order(self, total, status="confirmed"):
        order = Order.objects.create(
            client=self.client_profile, distributor=self.distributor, status=status
        )
        OrderItem.objects.create(
            order=order, product=self.product, sku="P-1", name="Краска",
            price=Decimal(total), quantity=1,
        )
        return order

    def _credit(self, amount):
        BonusTransaction.objects.create(
            client=self.client_profile, amount=Decimal(amount), kind="manual"
        )


class BonusCreditTests(_Base):
    """Начисление после согласования подарка."""

    def setUp(self):
        super().setUp()
        self.invitee = self._client("+79002223344", "7778889990", "Приглашённый")
        self.referral = Referral.objects.create(
            inviter=self.client_profile, invitee_inn=self.invitee.inn,
            invitee_name="Приглашённый", region=self.region.name,
            condition_met=True, gift="Сертификат на 5000 ₽",
            gift_amount=Decimal("5000"), gift_status="pending",
        )

    def test_approval_puts_money_on_the_account(self):
        self.referral.gift_status = "approved"
        self.referral.save(update_fields=["gift_status"])

        self.assertEqual(bonuses.balance(self.client_profile), Decimal("5000.00"))

    def test_pending_gift_gives_nothing(self):
        self.assertEqual(bonuses.balance(self.client_profile), Decimal("0.00"))

    def test_declined_gift_gives_nothing(self):
        self.referral.gift_status = "declined"
        self.referral.save(update_fields=["gift_status"])

        self.assertEqual(bonuses.balance(self.client_profile), Decimal("0.00"))

    def test_one_referral_pays_out_only_once(self):
        """Согласование могут повторить — денег от этого больше не станет."""
        self.referral.gift_status = "approved"
        self.referral.save(update_fields=["gift_status"])
        bonuses.credit_referral_bonus(self.referral)
        bonuses.credit_referral_bonus(self.referral)

        self.assertEqual(
            BonusTransaction.objects.filter(kind="referral").count(), 1
        )
        self.assertEqual(bonuses.balance(self.client_profile), Decimal("5000.00"))

    def test_non_monetary_gift_does_not_touch_the_account(self):
        # Отсрочка или статус — это не рубли, их отрабатывает дистрибьютор.
        self.referral.gift = "Отсрочка 14 дней"
        self.referral.gift_amount = Decimal("0")
        self.referral.gift_status = "approved"
        self.referral.save(update_fields=["gift", "gift_amount", "gift_status"])

        self.assertEqual(bonuses.balance(self.client_profile), Decimal("0.00"))


class BonusSpendTests(_Base):
    """Списание в счёт заказа."""

    def test_bonus_cannot_exceed_the_order(self):
        self._credit(5000)
        order = self._order(2000)

        applied = bonuses.debit_for_order(self.client_profile, order, Decimal("5000"))

        self.assertEqual(applied, Decimal("2000.00"))
        # Остаток должен уцелеть, а не сгореть.
        self.assertEqual(bonuses.balance(self.client_profile), Decimal("3000.00"))

    def test_bonus_cannot_exceed_the_balance(self):
        self._credit(1000)
        order = self._order(9000)

        applied = bonuses.debit_for_order(self.client_profile, order, Decimal("5000"))

        self.assertEqual(applied, Decimal("1000.00"))
        self.assertEqual(bonuses.balance(self.client_profile), Decimal("0.00"))

    def test_second_debit_for_same_order_is_ignored(self):
        """Повторный тап «Оплатить» не должен списать бонус дважды."""
        self._credit(5000)
        order = self._order(9000)

        bonuses.debit_for_order(self.client_profile, order, Decimal("3000"))
        bonuses.debit_for_order(self.client_profile, order, Decimal("2000"))

        self.assertEqual(bonuses.balance(self.client_profile), Decimal("2000.00"))

    def test_refund_returns_bonus_after_cancelled_payment(self):
        self._credit(5000)
        order = self._order(9000)
        bonuses.debit_for_order(self.client_profile, order, Decimal("5000"))

        bonuses.refund_for_order(order)

        self.assertEqual(bonuses.balance(self.client_profile), Decimal("5000.00"))

    def test_refund_is_not_repeated(self):
        self._credit(5000)
        order = self._order(9000)
        bonuses.debit_for_order(self.client_profile, order, Decimal("5000"))

        bonuses.refund_for_order(order)
        bonuses.refund_for_order(order)

        self.assertEqual(bonuses.balance(self.client_profile), Decimal("5000.00"))

    def test_zero_request_changes_nothing(self):
        self._credit(5000)
        order = self._order(9000)

        self.assertEqual(
            bonuses.debit_for_order(self.client_profile, order, Decimal("0")),
            Decimal("0.00"),
        )
        self.assertEqual(bonuses.balance(self.client_profile), Decimal("5000.00"))


class PayOrderWithBonusTests(_Base):
    """Оплата заказа: бонус уменьшает сумму, уходящую в ЮKassa."""

    def _pay(self, order, **body):
        return self.http.post(
            f"/api/orders/{order.id}/pay/",
            data=body,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )

    def test_bonus_reduces_the_amount_sent_to_provider(self):
        self._credit(5000)
        order = self._order(30000)

        with patch("api.services.payments.is_configured", return_value=True), \
             patch("api.services.payments.create_payment") as create:
            create.return_value = {
                "id": "pay-1", "status": "pending",
                "confirmation": {"confirmation_url": "https://pay.example/1"},
            }
            response = self._pay(order, useBonus=5000)

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(create.call_args.kwargs["amount"], Decimal("25000.00"))
        self.assertEqual(response.json()["bonusApplied"], 5000.0)

    def test_without_bonus_full_amount_goes_to_provider(self):
        self._credit(5000)
        order = self._order(30000)

        with patch("api.services.payments.is_configured", return_value=True), \
             patch("api.services.payments.create_payment") as create:
            create.return_value = {
                "id": "pay-2", "status": "pending",
                "confirmation": {"confirmation_url": "https://pay.example/2"},
            }
            self._pay(order)

        self.assertEqual(create.call_args.kwargs["amount"], Decimal("30000.00"))
        self.assertEqual(bonuses.balance(self.client_profile), Decimal("5000.00"))

    def test_use_bonus_true_spends_the_maximum(self):
        self._credit(5000)
        order = self._order(30000)

        with patch("api.services.payments.is_configured", return_value=True), \
             patch("api.services.payments.create_payment") as create:
            create.return_value = {
                "id": "pay-3", "status": "pending",
                "confirmation": {"confirmation_url": "https://pay.example/3"},
            }
            self._pay(order, useBonus=True)

        self.assertEqual(create.call_args.kwargs["amount"], Decimal("25000.00"))

    def test_bonus_covering_the_order_skips_the_provider(self):
        """Платить нечего — в ЮKassa идти незачем, заказ сразу оплачен."""
        self._credit(5000)
        order = self._order(4000)

        with patch("api.services.payments.create_payment") as create:
            response = self._pay(order, useBonus=True)

        self.assertEqual(response.status_code, 201, response.content)
        create.assert_not_called()

        order.refresh_from_db()
        self.assertEqual(order.status, "paid")
        self.assertIsNotNone(order.paid_at)
        self.assertEqual(bonuses.balance(self.client_profile), Decimal("1000.00"))

    def test_provider_failure_returns_the_bonus(self):
        from api.services.payments import PaymentProviderError

        self._credit(5000)
        order = self._order(30000)

        with patch("api.services.payments.is_configured", return_value=True), \
             patch("api.services.payments.create_payment",
                   side_effect=PaymentProviderError("boom")):
            response = self._pay(order, useBonus=5000)

        self.assertEqual(response.status_code, 502)
        # Оплата не создана — бонус не должен сгореть.
        self.assertEqual(bonuses.balance(self.client_profile), Decimal("5000.00"))

    def test_unconfigured_provider_returns_the_bonus(self):
        self._credit(5000)
        order = self._order(30000)

        with patch("api.services.payments.is_configured", return_value=False):
            response = self._pay(order, useBonus=5000)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(bonuses.balance(self.client_profile), Decimal("5000.00"))

    def test_negative_bonus_is_rejected(self):
        self._credit(5000)
        order = self._order(30000)

        response = self._pay(order, useBonus=-100)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(bonuses.balance(self.client_profile), Decimal("5000.00"))

    def test_unconfirmed_order_cannot_be_paid(self):
        # Бонус не должен списаться с заказа, который ещё не подтверждён.
        self._credit(5000)
        order = self._order(30000, status="new")

        response = self._pay(order, useBonus=5000)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(bonuses.balance(self.client_profile), Decimal("5000.00"))


class BonusAccountEndpointTests(_Base):
    def test_balance_and_history_are_returned(self):
        self._credit(5000)
        order = self._order(2000)
        bonuses.debit_for_order(self.client_profile, order, Decimal("2000"))

        response = self.http.get(
            "/api/bonus-account/", HTTP_AUTHORIZATION=f"Bearer {self.token}"
        )
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["balance"], 3000.0)
        self.assertEqual(len(data["results"]), 2)

    def test_balance_is_visible_on_the_dashboard(self):
        self._credit(5000)

        response = self.http.get(
            "/api/dashboard/", HTTP_AUTHORIZATION=f"Bearer {self.token}"
        )

        self.assertEqual(response.json()["bonusBalance"], 5000.0)

    def test_other_clients_bonuses_do_not_leak(self):
        other = self._client("+79005554433", "1231231231", "Чужой")
        BonusTransaction.objects.create(
            client=other, amount=Decimal("9999"), kind="manual"
        )

        response = self.http.get(
            "/api/bonus-account/", HTTP_AUTHORIZATION=f"Bearer {self.token}"
        )

        self.assertEqual(response.json()["balance"], 0.0)
        self.assertEqual(response.json()["results"], [])
