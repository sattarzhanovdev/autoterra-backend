"""Защита ручной заявки на приглашённое СТО.

Форма «Пригласить СТО» создаёт запись, которая сама по себе даёт право на
подарок. Проверок в create_referral не было вообще: можно было заявиться на
свой же ИНН, дважды на один и тот же и поверх чужой заявки.
"""

import json

from django.contrib.auth.models import User
from django.test import Client, TestCase

from .models import AuthToken, ClientProfile, Distributor, Referral, Region


class ReferralClaimGuardTests(TestCase):
    def setUp(self):
        self.http = Client()
        self.distributor = Distributor.objects.create(
            name="Дист", inn="1112223334", phone="1", email="d@e.co"
        )
        self.region = Region.objects.create(
            code="59", name="Пермь - Урал", distributor=self.distributor
        )
        self.inviter = self._client("+79001110001", "7707083893", "Пригласивший")
        self.token = AuthToken.objects.create(key="inviter-token", user=self.inviter.user)

    def _client(self, username, inn, company="Компания"):
        user = User.objects.create_user(username=username, password="pw")
        return ClientProfile.objects.create(
            user=user, inn=inn, company_name=company, contact_name="Иван",
            phone=username, region=self.region, city="Пермь", distributor=self.distributor,
        )

    def _create(self, inn, name="СТО Ромашка"):
        return self.http.post(
            "/api/referrals/create/",
            data=json.dumps({"inviteeInn": inn, "inviteeName": name}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.key}",
        )

    def test_valid_claim_is_created(self):
        response = self._create("7736050003")

        self.assertEqual(response.status_code, 201)
        referral = Referral.objects.get(invitee_inn="7736050003")
        self.assertEqual(referral.inviter, self.inviter)
        # region — строка из справочника, а не сам объект Region.
        self.assertEqual(referral.region, "Пермь - Урал")

    def test_wrong_length_inn_is_rejected(self):
        """Проверяем только длину: контрольную сумму намеренно не считаем —
        в базе полно СТО с номерами, которые её не проходят."""
        for inn in ("666", "12345678901", "абвгдеёжзи"):
            response = self._create(inn)
            self.assertEqual(response.status_code, 400, inn)
        self.assertFalse(Referral.objects.exists())

    def test_inn_without_valid_checksum_is_accepted(self):
        # 6666666666 — контрольную сумму не проходит, но это нормальный ввод.
        self.assertEqual(self._create("6666666666").status_code, 201)

    def test_empty_name_is_rejected(self):
        response = self._create("7736050003", name="   ")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Referral.objects.exists())

    def test_cannot_invite_self(self):
        """Иначе бонус капал бы за собственные заказы."""
        response = self._create(self.inviter.inn)

        self.assertEqual(response.status_code, 400)
        self.assertIn("самого себя", response.json()["detail"])
        self.assertFalse(Referral.objects.exists())

    def test_cannot_claim_twice(self):
        self.assertEqual(self._create("7736050003").status_code, 201)

        response = self._create("7736050003")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(Referral.objects.filter(invitee_inn="7736050003").count(), 1)

    def test_cannot_claim_what_someone_else_claimed(self):
        other = self._client("+79001110002", "7736207543", "Другой")
        Referral.objects.create(
            inviter=other, invitee_inn="7736050003", invitee_name="СТО", region=""
        )

        response = self._create("7736050003")

        self.assertEqual(response.status_code, 409)
        self.assertIn("другим участником", response.json()["detail"])
        self.assertEqual(Referral.objects.filter(invitee_inn="7736050003").count(), 1)

    def test_cannot_claim_already_registered_client(self):
        """Перехват: вписать ИНН действующего клиента и забрать бонус за чужого."""
        self._client("+79001110003", "7736050003", "Уже с нами")

        response = self._create("7736050003")

        self.assertEqual(response.status_code, 409)
        self.assertIn("уже зарегистрировано", response.json()["detail"])
        self.assertFalse(Referral.objects.exists())

    def test_unauthorized_is_401(self):
        response = self.http.post(
            "/api/referrals/create/",
            data=json.dumps({"inviteeInn": "7736050003", "inviteeName": "СТО"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)


class ReferralCodeBeatsManualClaimTests(TestCase):
    """Регистрация по коду — доказательство сильнее ручной заявки."""

    def setUp(self):
        self.http = Client()
        self.distributor = Distributor.objects.create(
            name="Дист", inn="1112223334", phone="1", email="d@e.co"
        )
        self.region = Region.objects.create(
            code="59", name="Пермь - Урал", distributor=self.distributor
        )

    def _client(self, username, inn, company="Компания"):
        user = User.objects.create_user(username=username, password="pw")
        return ClientProfile.objects.create(
            user=user, inn=inn, company_name=company, contact_name="Иван",
            phone=username, region=self.region, city="Пермь", distributor=self.distributor,
        )

    def test_squatter_claim_is_dropped(self):
        real = self._client("+79001110001", "7707083893", "Настоящий")
        squatter = self._client("+79001110002", "7736207543", "Перехватчик")
        Referral.objects.create(
            inviter=squatter, invitee_inn="7736050003", invitee_name="СТО", region=""
        )

        from .views import _link_referral

        newcomer = self._client("+79001110003", "7736050003", "Новичок")
        _link_referral(newcomer, real.referral_code)

        remaining = Referral.objects.filter(invitee_inn="7736050003")
        self.assertEqual(remaining.count(), 1)
        self.assertEqual(remaining.first().inviter, real)

    def test_approved_gift_is_not_taken_away(self):
        """Согласованный подарок не отбираем, даже если пришёл по чужому коду."""
        real = self._client("+79001110001", "7707083893", "Настоящий")
        other = self._client("+79001110002", "7736207543", "Другой")
        Referral.objects.create(
            inviter=other, invitee_inn="7736050003", invitee_name="СТО",
            region="", gift_status="approved", condition_met=True,
        )

        from .views import _link_referral

        newcomer = self._client("+79001110003", "7736050003", "Новичок")
        _link_referral(newcomer, real.referral_code)

        self.assertEqual(Referral.objects.filter(invitee_inn="7736050003").count(), 2)


class ClaimNeedsInviteeConfirmationTests(TestCase):
    """Ручная заявка не даёт подарка, пока приглашённый её не подтвердил."""

    def setUp(self):
        self.http = Client()
        self.distributor = Distributor.objects.create(
            name="Дист", inn="1112223334", phone="1", email="d@e.co"
        )
        self.region = Region.objects.create(
            code="59", name="Пермь - Урал", distributor=self.distributor
        )
        self.inviter = self._client("+79001110001", "7707083893", "Пригласивший")
        self.token = AuthToken.objects.create(key="inviter-token", user=self.inviter.user)

    def _client(self, username, inn, company="Компания"):
        user = User.objects.create_user(username=username, password="pw")
        return ClientProfile.objects.create(
            user=user, inn=inn, company_name=company, contact_name="Иван",
            phone=username, region=self.region, city="Пермь", distributor=self.distributor,
        )

    def _claim(self, inn="7736050003"):
        response = self.http.post(
            "/api/referrals/create/",
            data=json.dumps({"inviteeInn": inn, "inviteeName": "СТО"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.key}",
        )
        self.assertEqual(response.status_code, 201)
        return Referral.objects.get(invitee_inn=inn)

    def test_manual_claim_starts_pending(self):
        referral = self._claim()
        self.assertEqual(referral.confirmation, "pending")
        self.assertFalse(referral.counts_toward_bonus)

    def test_registration_by_code_needs_no_confirmation(self):
        from .views import _link_referral

        newcomer = self._client("+79001110004", "7736050003", "Новичок")
        referral = _link_referral(newcomer, self.inviter.referral_code)

        self.assertEqual(referral.confirmation, "auto")
        self.assertTrue(referral.counts_toward_bonus)

    def test_code_confirms_earlier_manual_claim(self):
        """Заявили заранее, потом клиент пришёл по коду того же человека."""
        self._claim()

        from .views import _link_referral

        newcomer = self._client("+79001110004", "7736050003", "Новичок")
        referral = _link_referral(newcomer, self.inviter.referral_code)

        self.assertEqual(referral.confirmation, "auto")
        self.assertIsNotNone(referral.confirmed_at)

    def test_registration_returns_pending_claim(self):
        self._claim(inn="7736050003")

        response = self.http.post(
            "/api/register/",
            data=json.dumps({
                "username": "+79001110009",
                "password": "Str0ngPass!",
                "inn": "7736050003",
                "company_name": "СТО Новичок",
                "contact_name": "Пётр",
                "region_id": self.region.id,
                "store_address": "Пермь, ул. Ленина 1",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        claim = response.json()["pendingReferral"]
        self.assertEqual(claim["inviterName"], "Пригласивший")

    def test_invitee_confirms(self):
        referral = self._claim()
        invitee = self._client("+79001110004", "7736050003", "Новичок")
        token = AuthToken.objects.create(key="invitee-token", user=invitee.user)

        response = self.http.post(
            f"/api/referrals/{referral.id}/confirm/",
            data=json.dumps({"confirmed": True}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token.key}",
        )

        self.assertEqual(response.status_code, 200)
        referral.refresh_from_db()
        self.assertEqual(referral.confirmation, "confirmed")
        self.assertTrue(referral.counts_toward_bonus)

    def test_invitee_declines(self):
        referral = self._claim()
        invitee = self._client("+79001110004", "7736050003", "Новичок")
        token = AuthToken.objects.create(key="invitee-token", user=invitee.user)

        response = self.http.post(
            f"/api/referrals/{referral.id}/confirm/",
            data=json.dumps({"confirmed": False}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token.key}",
        )

        self.assertEqual(response.status_code, 200)
        referral.refresh_from_db()
        self.assertEqual(referral.confirmation, "declined")
        self.assertFalse(referral.counts_toward_bonus)

    def test_stranger_cannot_confirm(self):
        """Иначе пригласивший подтвердил бы собственную заявку сам."""
        referral = self._claim()
        stranger = self._client("+79001110005", "7736207543", "Посторонний")
        token = AuthToken.objects.create(key="stranger-token", user=stranger.user)

        response = self.http.post(
            f"/api/referrals/{referral.id}/confirm/",
            data=json.dumps({"confirmed": True}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token.key}",
        )

        self.assertEqual(response.status_code, 403)
        referral.refresh_from_db()
        self.assertEqual(referral.confirmation, "pending")

    def test_cannot_decide_twice(self):
        referral = self._claim()
        invitee = self._client("+79001110004", "7736050003", "Новичок")
        token = AuthToken.objects.create(key="invitee-token", user=invitee.user)
        body = json.dumps({"confirmed": True})

        first = self.http.post(
            f"/api/referrals/{referral.id}/confirm/", data=body,
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token.key}",
        )
        second = self.http.post(
            f"/api/referrals/{referral.id}/confirm/", data=body,
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token.key}",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
