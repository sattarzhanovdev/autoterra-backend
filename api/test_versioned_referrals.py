from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.test import override_settings
from .test_referral_bonus_percent import _Base
from .models import BonusTransaction, ReferralRuleVersion, ReferralAccrual
from .services import referral_bonus as rb
from .services.bonuses import balance
from .services.ai_answer_policy import clean_client_answer


class VersionedReferralTests(_Base):
    def test_new_rules_only_apply_to_new_confirmed_operation(self):
        self._referral()
        before = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
        after = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
        with patch('django.utils.timezone.now', return_value=before):
            self._purchase(self.invitee, 100000)
        first = BonusTransaction.objects.get(kind='referral')
        self.assertEqual(first.amount, Decimal(1000))
        rule = ReferralRuleVersion.objects.create(effective_from=datetime(2026, 11, 1, tzinfo=timezone.utc), tiers=[['0', '2']], activity_min=10000)
        with patch('django.utils.timezone.now', return_value=after):
            self._purchase(self.invitee, 50000)
            self.assertEqual(balance(self.inviter), Decimal(2000))
            self.assertEqual(rb.accrue(self.inviter.referrals.get()), Decimal(0))
            rule.tiers = [['0', '5']]
            with self.assertRaises(ValidationError):
                rule.save()
        first.refresh_from_db()
        self.assertEqual(first.amount, Decimal(1000))
        self.assertEqual(ReferralAccrual.objects.count(), 2)
        self.assertEqual(BonusTransaction.objects.count(), 2)
        self.assertEqual(BonusTransaction.objects.latest('pk').referral_event.version_id, rule.pk)

    @override_settings(REFERRAL_BONUS_TIERS=[(0, '99')])
    def test_settings_change_cannot_reprice_confirmed_operations(self):
        referral = self._referral()
        self._purchase(self.invitee, 100000)
        self.assertEqual(balance(self.inviter), Decimal(1000))
        self.assertEqual(rb.accrue(referral), Decimal(0))
        self.assertEqual(balance(self.inviter), Decimal(1000))

    def test_citation_cleanup_preserves_useful_urls(self):
        text = 'Инструкция: [сайт производителя](https://manufacturer.test/help). https://help.example.test\n\nИсточники:\n[1] TDS.pdf https://docs.example.test'
        result = clean_client_answer(text, [{'type': 'document', 'filename': 'TDS.pdf', 'url': 'https://docs.example.test'}])
        self.assertIn('[сайт производителя](https://manufacturer.test/help)', result)
        self.assertIn('https://help.example.test', result)
        self.assertNotIn('TDS.pdf', result)
        self.assertNotIn('Источники', result)
