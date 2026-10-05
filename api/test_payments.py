"""Financial regressions: provider verification, retries and bonus reservations."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone

from .test_bonus_account import _Base
from .models import Payment, BonusTransaction, OrderItem
from .services import payments, bonuses
from .views import _apply_provider_payment


@override_settings(YOOKASSA_SHOP_ID="shop-test", YOOKASSA_SECRET_KEY="test-only")
class PaymentTests(_Base):
    def setUp(self):
        super().setUp()
        self.order = self._order(1000)

    def response(self, status="pending", amount="1000.00", **updates):
        result = {
            "id": "provider-1", "status": status, "paid": status == "succeeded",
            "amount": {"value": amount, "currency": "RUB"},
            "metadata": {"order_id": str(self.order.pk)},
            "recipient": {"account_id": "shop-test"},
            "confirmation": {"confirmation_url": "https://yoomoney.ru/pay/test"},
        }
        result.update(updates)
        return result

    def pay(self, **body):
        return self.http.post(f"/api/orders/{self.order.pk}/pay/", body,
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.token}")

    def pending(self, amount=1000):
        return Payment.objects.create(order=self.order, amount=amount,
            provider_payment_id="provider-1", idempotence_key="fixed-key")

    @override_settings(YOOKASSA_RECEIPT_EMAIL="receipts@example.com")
    def test_receipt_uses_client_email_and_product_fields(self):
        self.client_profile.user.email = "buyer@example.com"
        self.client_profile.user.save(update_fields=["email"])
        body = payments.payment_body(self.order, Decimal("1000.00"))
        self.assertEqual(body["receipt"]["customer"], {"email": "buyer@example.com"})
        self.assertEqual(body["receipt"]["items"], [{
            "description": "Краска", "quantity": 1,
            "amount": {"value": "1000.00", "currency": "RUB"},
            "vat_code": 7, "payment_mode": "full_prepayment",
            "payment_subject": "commodity", "measure": "piece",
        }])

    @override_settings(YOOKASSA_RECEIPT_EMAIL="receipts@example.com")
    def test_receipt_fallback_and_bonus_split_match_payment_exactly(self):
        first = self.order.items.get()
        first.price = Decimal("1.01")
        first.quantity = 3
        first.save(update_fields=["price", "quantity"])
        OrderItem.objects.create(order=self.order, product=self.product, sku="P-2",
                                 name="Лак", price=Decimal("2.02"), quantity=2)
        body = payments.payment_body(self.order, Decimal("5.04"))
        self.assertEqual(body["receipt"]["customer"], {"email": "receipts@example.com"})
        items = body["receipt"]["items"]
        self.assertEqual({item["description"] for item in items}, {"Краска", "Лак"})
        self.assertEqual(sum(item["quantity"] for item in items), 5)
        self.assertEqual(sum(Decimal(item["amount"]["value"]) * item["quantity"] for item in items),
                         Decimal(body["amount"]["value"]))
        self.assertTrue(all(Decimal(item["amount"]["value"]) > 0 for item in items))

    def test_unrepresentable_bonus_payment_rolls_back_bonus(self):
        self.order.items.update(price=Decimal("1.00"), quantity=2)
        self._credit(Decimal("1.99"))
        with patch.object(payments, "create_payment") as create:
            self.assertEqual(self.pay(useBonus="1.99").status_code, 400)
            create.assert_not_called()
        self.assertEqual(bonuses.balance(self.client_profile), Decimal("1.99"))
        self.assertFalse(Payment.objects.exists())

    def webhook(self, event="payment.succeeded"):
        return self.http.post("/api/payments/yookassa/webhook/",
            {"event": event, "object": {"id": "provider-1", "status": "succeeded"}},
            content_type="application/json")

    def test_forged_success_cannot_override_provider_pending(self):
        self.pending()
        with patch.object(payments, "fetch_payment", return_value=self.response()):
            self.assertEqual(self.webhook().status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "confirmed")

    def test_verification_failure_and_missing_keys_fail_closed(self):
        self.pending()
        for error in [payments.PaymentProviderError("offline"), payments.PaymentConfigError("missing")]:
            with patch.object(payments, "fetch_payment", side_effect=error):
                self.assertEqual(self.webhook().status_code, 503)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "confirmed")

    def test_mismatched_payment_is_never_accepted(self):
        self.pending()
        for updates in [dict(amount={"value": "1", "currency": "RUB"}),
                        dict(amount={"value": "1000", "currency": "USD"}),
                        dict(metadata={"order_id": "wrong"}),
                        dict(recipient={"account_id": "wrong"}), dict(id="wrong"), dict(paid=False)]:
            with patch.object(payments, "fetch_payment", return_value=self.response("succeeded", **updates)):
                self.assertEqual(self.webhook().status_code, 503)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "confirmed")

    def test_success_is_applied_once_and_stale_cancel_cannot_undo_it(self):
        payment = self.pending()
        with patch("api.views._notify_client_order") as notify:
            _apply_provider_payment(payment.pk, self.response("succeeded"))
            _apply_provider_payment(payment.pk, self.response("succeeded"))
            _apply_provider_payment(payment.pk, self.response("canceled"))
            self.assertEqual(notify.call_count, 1)
        self.order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(self.order.status, "paid")
        self.assertEqual(payment.status, "succeeded")

    def test_timeout_reuses_key_body_and_bonus(self):
        self._credit(200)
        with patch.object(payments, "create_payment", side_effect=payments.PaymentUncertainError("timeout")) as create:
            self.assertEqual(self.pay(useBonus=200).status_code, 503)
            first = create.call_args.kwargs.copy()
        self.assertEqual(bonuses.balance(self.client_profile), Decimal("0"))
        with patch.object(payments, "create_payment", return_value=self.response(amount="800.00")) as create:
            self.assertEqual(self.pay(useBonus=0).status_code, 201)
            self.assertEqual(create.call_args.kwargs["idempotence_key"], first["idempotence_key"])
            self.assertEqual(create.call_args.kwargs["request_body"], first["request_body"])
            receipt = first["request_body"]["receipt"]
            self.assertEqual(receipt["items"][0]["amount"]["value"], "800.00")
            self.assertEqual(sum(Decimal(item["amount"]["value"]) * item["quantity"]
                                 for item in receipt["items"]), Decimal("800.00"))
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(BonusTransaction.objects.filter(kind="order").count(), 1)

    def test_cancellation_refunds_once_and_retry_debits_again(self):
        self._credit(200)
        bonuses.debit_for_order(self.client_profile, self.order, Decimal(200))
        self.pending(800)
        with patch.object(payments, "fetch_payment", return_value=self.response("canceled", amount="800.00")):
            self.webhook("payment.canceled")
            self.webhook("payment.canceled")
        self.assertEqual(bonuses.balance(self.client_profile), Decimal(200))
        with patch.object(payments, "create_payment", return_value=self.response(amount="800.00", id="provider-2")):
            self.assertEqual(self.pay(useBonus=200).status_code, 201)
        self.assertEqual(bonuses.balance(self.client_profile), Decimal(0))

    def test_pending_payment_blocks_cancellation(self):
        self.pending()
        result = self.http.post(f"/api/orders/{self.order.pk}/cancel/",
            HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(result.status_code, 409)

    def test_detail_reconciles_missing_webhook(self):
        self.pending()
        with patch.object(payments, "fetch_payment", return_value=self.response("succeeded")):
            result = self.http.get(f"/api/orders/{self.order.pk}/",
                HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["order"]["status"], "paid")

    def test_expired_unknown_attempt_cannot_create_another_charge(self):
        payment = Payment.objects.create(order=self.order, amount=1000, idempotence_key="old")
        Payment.objects.filter(pk=payment.pk).update(created_at=timezone.now()-timedelta(days=2))
        with patch.object(payments, "create_payment") as create:
            self.assertEqual(self.pay().status_code, 409)
            create.assert_not_called()

    def test_unaccepted_adjustment_and_nonfinite_bonus_rejected(self):
        for value in ["NaN", "Infinity", "-Infinity"]:
            self.assertEqual(self.pay(useBonus=value).status_code, 400)
        self.order.status = "adjusted"
        self.order.save()
        self.assertEqual(self.pay().status_code, 400)

    def test_duplicate_tap_fetches_existing_payment(self):
        self.pending()
        with patch.object(payments, "fetch_payment", return_value=self.response()), patch.object(payments, "create_payment") as create:
            self.assertEqual(self.pay().status_code, 201)
            create.assert_not_called()
        self.assertEqual(Payment.objects.count(), 1)

    def test_other_client_cannot_pay(self):
        other = self._client("other", "2223334445")
        self.order.client = other
        self.order.save()
        self.assertEqual(self.pay().status_code, 404)

    def test_reconcile_recovers_creation_response_after_timeout(self):
        from django.core.management import call_command
        from io import StringIO
        with patch.object(payments, "create_payment", side_effect=payments.PaymentUncertainError("timeout")):
            self.pay()
        payment = Payment.objects.get(order=self.order)
        with patch.object(payments, "create_payment", return_value=self.response("succeeded")) as create:
            call_command("reconcile_payments", stdout=StringIO())
            self.assertEqual(create.call_args.kwargs["idempotence_key"], payment.idempotence_key)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "paid")

    def test_reentrant_tap_during_create_reuses_committed_attempt(self):
        from .views import _prepare_order_payment
        def create(**kwargs):
            second = _prepare_order_payment(self.client_profile, self.order.pk, {"useBonus": 0})
            self.assertEqual(second.idempotence_key, kwargs["idempotence_key"])
            self.assertEqual(second.amount, kwargs["amount"])
            return self.response()
        with patch.object(payments, "create_payment", side_effect=create):
            self.assertEqual(self.pay().status_code, 201)
        self.assertEqual(Payment.objects.count(), 1)

    def test_failed_retry_does_not_refund_an_unknown_previous_charge(self):
        self._credit(200)
        with patch.object(payments, "create_payment", side_effect=payments.PaymentUncertainError("timeout")):
            self.pay(useBonus=200)
        with patch.object(payments, "create_payment", side_effect=payments.PaymentProviderError("401")):
            self.assertEqual(self.pay().status_code, 503)
        self.assertEqual(Payment.objects.get(order=self.order).status, "pending")
        self.assertEqual(bonuses.balance(self.client_profile), Decimal(0))

    def test_malformed_provider_response_preserves_attempt_and_bonus(self):
        self._credit(200)
        invalid = [None, [], "invalid", self.response(amount="800.00", id=123),
                   self.response(amount="800.00", confirmation=["broken"]),
                   self.response(amount="800.00", confirmation={"confirmation_url": "javascript:alert(1)"}),
                   self.response(amount="800.00", confirmation={"confirmation_url": 123})]
        for response in invalid:
            with self.subTest(response=response), patch.object(payments, "create_payment", return_value=response):
                self.assertEqual(self.pay(useBonus=200).status_code, 503)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Payment.objects.get().status, "pending")
        self.assertEqual(bonuses.balance(self.client_profile), Decimal(0))

    def test_refresh_without_confirmation_preserves_payment_link(self):
        payment = self.pending()
        payment.confirmation_url = "https://yoomoney.ru/pay/test"
        payment.save()
        response = self.response()
        response.pop("confirmation")
        _apply_provider_payment(payment.pk, response)
        payment.refresh_from_db()
        self.assertEqual(payment.confirmation_url, "https://yoomoney.ru/pay/test")

    def test_terminal_response_clears_payment_link(self):
        payment = self.pending()
        _apply_provider_payment(payment.pk, self.response("succeeded"))
        payment.refresh_from_db()
        self.assertEqual(payment.confirmation_url, "")

    def test_unrepresentable_order_total_does_not_debit_or_dispatch(self):
        self._credit(200)
        self.order.items.update(price=Decimal("9999999999.99"), quantity=2)
        with patch.object(payments, "create_payment") as create:
            self.assertEqual(self.pay(useBonus=200).status_code, 400)
            create.assert_not_called()
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(bonuses.balance(self.client_profile), Decimal(200))

    def test_manual_review_attempt_does_not_starve_reconciliation_batch(self):
        from django.core.management import call_command, CommandError
        from io import StringIO

        old_order = self._order(100)
        old = Payment.objects.create(order=old_order, amount=100, idempotence_key="old")
        Payment.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=2))
        self.pending()
        with patch.object(payments, "fetch_payment", return_value=self.response("succeeded")):
            with self.assertRaises(CommandError):
                call_command("reconcile_payments", limit=1, stdout=StringIO(), stderr=StringIO())
        self.order.refresh_from_db()
        old.refresh_from_db()
        self.assertEqual(self.order.status, "paid")
        self.assertEqual(old.status, "pending")
