from decimal import Decimal
from django.core.exceptions import PermissionDenied, ValidationError
from django.contrib.auth.models import User
from django.test import override_settings
from .test_bonus_account import _Base
from .models import AndroidRelease, AuditLog, BonusTransaction
from .services import bonuses, referral_bonus
from .services.ai_answer_policy import clean_client_answer


class BonusLimitAndVersionTests(_Base):
    def test_all_referral_boundaries_are_per_invitee(self):
        for volume, rate in [(0, '.5'), (99999, '.5'), (100000, '1'), (199999, '1'), (200000, '1.5'), (399999, '1.5'), (400000, '2'), (799999, '2'), (800000, '2.5')]:
            with self.subTest(volume=volume):
                self.assertEqual(referral_bonus.rate_for(Decimal(volume)), Decimal(rate))
        self.assertEqual(referral_bonus.rate_for(300000), Decimal('1.5'))
        self.assertEqual(referral_bonus.rate_for(50000), Decimal('.5'))

    def test_quote_never_debits_and_clamps_tampered_amount(self):
        self._credit(10000)
        result = bonuses.quote(self.client_profile, Decimal('50000'), '99999')
        self.assertEqual(result['maxAllowed'], '5000.00')
        self.assertEqual(result['applied'], '5000.00')
        self.assertEqual(result['payableTotal'], '45000.00')
        self.assertEqual(bonuses.balance(self.client_profile), Decimal('10000'))
        self.assertEqual(BonusTransaction.objects.count(), 1)

    def test_balance_and_fractional_limit(self):
        self._credit('2')
        self.assertEqual(bonuses.quote(self.client_profile, Decimal('100'), '50')['applied'], '2.00')
        self.assertEqual(bonuses.spendable_for_order(self.client_profile, Decimal('1.99')), Decimal('.19'))
        for amount in ['NaN', 'Infinity', '-1', '1.001']:
            with self.assertRaises(ValueError):
                bonuses.quote(self.client_profile, Decimal(100), amount)

    def test_quote_api_uses_discounted_total_without_spending(self):
        self._credit(5000)
        response = self.http.post('/api/orders/quote/', data={'items': [{'productId': str(self.product.pk), 'quantity': 1}], 'bonusAmount': '99999'}, content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {self.token}')
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        total = Decimal(data['orderDiscount']['total'])
        self.assertLessEqual(Decimal(data['bonus']['applied']), total * Decimal('.1'))
        self.assertEqual(bonuses.balance(self.client_profile), Decimal(5000))

    def test_payment_rejects_invalid_bonus_amounts_without_debit(self):
        self._credit(5000)
        order = self._order(1000)
        for amount in ['1E+999', 'NaN', '-1', '0.001']:
            response = self.http.post(f'/api/orders/{order.pk}/pay/', {'bonusAmount': amount}, content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {self.token}')
            self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(bonuses.balance(self.client_profile), Decimal(5000))

    def test_retry_after_refund_debits_once_per_active_attempt(self):
        self._credit(10000)
        order = self._order(50000)
        self.assertEqual(bonuses.debit_for_order(self.client_profile, order, Decimal(99999)), Decimal(5000))
        self.assertEqual(bonuses.debit_for_order(self.client_profile, order, Decimal(99999)), Decimal(5000))
        self.assertEqual(bonuses.balance(self.client_profile), Decimal(5000))
        bonuses.refund_for_order(order)
        bonuses.refund_for_order(order)
        self.assertEqual(bonuses.balance(self.client_profile), Decimal(10000))
        bonuses.debit_for_order(self.client_profile, order, Decimal(1000))
        bonuses.debit_for_order(self.client_profile, order, Decimal(1000))
        self.assertEqual(bonuses.balance(self.client_profile), Decimal(9000))

    def test_manual_debit_audited_and_validated(self):
        self._credit(10000)
        admin = User.objects.create_superuser('bonus-admin', 'admin@example.test', 'pw')
        for amount, reason in [('10001', 'reason'), ('1', ''), ('-1', 'reason')]:
            with self.assertRaises(ValueError):
                bonuses.manual_debit(self.client_profile, amount, reason, admin)
        with self.assertRaises(PermissionDenied):
            bonuses.manual_debit(self.client_profile, '1', 'reason', self.client_profile.user)
        entry = bonuses.manual_debit(self.client_profile, '10000', 'Выдан бонус вручную', admin)
        self.assertEqual(entry.kind, 'manual')
        self.assertEqual(bonuses.balance(self.client_profile), Decimal(0))
        self.assertTrue(AuditLog.objects.filter(object_id=str(entry.pk), action='Manual bonus debit').exists())

    def test_admin_form_previews_balance_and_records_debit(self):
        self._credit(1000)
        admin = User.objects.create_superuser('form-admin', 'a@example.test', 'pw')
        self.http.force_login(admin)
        url = '/admin/api/bonustransaction/debit/'
        preview = self.http.post(url, {'client': self.client_profile.pk, 'preview': '1'})
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, '1000')
        self.assertEqual(bonuses.balance(self.client_profile), Decimal(1000))
        response = self.http.post(url, {'client': self.client_profile.pk, 'amount': '500.00', 'reason': 'Выдан бонус вручную'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(bonuses.balance(self.client_profile), Decimal(500))
        self.assertTrue(AuditLog.objects.filter(action='Manual bonus debit', user=admin).exists())

    def test_versions_independent_channels_and_mandatory_boundary(self):
        release = AndroidRelease.objects.create(store='google_play', latest_version_name='1.0.1', latest_build_number=23, minimum_supported_build_number=22, store_url='https://play.google.com/store/apps/details?id=com.autoterra.autoterra')
        release.full_clean()
        for installed, available, mandatory in [(21, True, True), (22, True, False), (23, False, False), (24, False, False)]:
            data = self.http.get(f'/api/app-version/?store=google_play&build={installed}').json()
            self.assertEqual((data['updateAvailable'], data['forceUpdate']), (available, mandatory))
        self.assertFalse(self.http.get('/api/app-version/?store=rustore&build=1').json()['updateAvailable'])
        self.assertEqual(self.http.get('/api/app-version/?store=bad&build=1').status_code, 400)
        release.minimum_supported_build_number = 24
        with self.assertRaises(ValidationError):
            release.full_clean()

    def test_ai_presentation_removes_sources_without_removing_metadata(self):
        sources = [{'type': 'document', 'title': 'TDS File.pdf'}]
        answer = clean_client_answer('Используйте грунт [1].\n\nИсточники:\n[1] TDS File.pdf https://example.test', sources)
        self.assertEqual(answer, 'Используйте грунт.')
        self.assertEqual(sources[0]['title'], 'TDS File.pdf')
