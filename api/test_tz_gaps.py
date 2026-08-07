"""Пункты ТЗ, которых не хватало: согласование подарка, архив, материалы.

Три отдельные истории:
  * п. 7 шаг 6 — подарок в виде скидки или отсрочки согласует дистрибьютор;
  * п. 6 — «архивный» клиент это не «заблокированный»;
  * п. 10 — обучающие материалы это не карточки базы знаний;
  * п. 9 шаг 5 — черновик карточки появляется сам, а не по галочке эксперта.
"""

from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

from .models import (
    AuthToken,
    ClientProfile,
    Distributor,
    ExpertTicket,
    KnowledgeCard,
    LearningMaterial,
    Notification,
    Profile,
    Purchase,
    Referral,
    Region,
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

    def _client_profile(self, username, inn, company="Компания"):
        user = User.objects.create_user(username=username, password="pw")
        return ClientProfile.objects.create(
            user=user, inn=inn, company_name=company, contact_name="Иван",
            phone=username, region=self.region, city="Москва",
            distributor=self.distributor, status="active",
        )

    def _token(self, user, key):
        return AuthToken.objects.create(key=key, user=user).key


@override_settings(REFERRAL_BONUS_THRESHOLD=30000, REFERRAL_BONUS_GIFT="Отсрочка 14 дней")
class ReferralGiftApprovalTests(_Base):
    """п. 7 шаг 6: подарок-скидка согласуется с дистрибьютором."""

    def setUp(self):
        super().setUp()
        self.inviter = self._client_profile("+79001110000", "5556667778", "Пригласивший")
        self.inviter_token = self._token(self.inviter.user, "inviter-token")
        self.invitee = self._client_profile("+79002223344", "7778889990", "Приглашённый")
        self.referral = Referral.objects.create(
            inviter=self.inviter, invitee_inn=self.invitee.inn,
            invitee_name=self.invitee.company_name, region=self.region.name,
        )

        dist_user = User.objects.create_user(username="dist", password="pw")
        Profile.objects.update_or_create(user=dist_user, defaults={"role": "distributor"})
        self.distributor.user = dist_user
        self.distributor.save(update_fields=["user"])
        self.dist_token = self._token(dist_user, "dist-token")

    def _reach_threshold(self):
        """Заводит подарок, ждущий согласования.

        Автоматически по порогу он больше не появляется — регулярное
        вознаграждение теперь процент от оборота, который капает на счёт сам
        (см. test_referral_bonus_percent). Согласование осталось для разовых
        подарков: их назначает дистрибьютор или менеджер вручную.
        """
        Purchase.objects.create(
            client=self.invitee, distributor=self.distributor,
            document_number="D1", date="2026-01-01",
            total_amount=Decimal(35_000), status="verified",
        )
        self.referral.sync_from_invitee()
        self.referral.gift = "Отсрочка 14 дней"
        self.referral.gift_amount = Decimal(5000)
        self.referral.gift_status = "pending"
        self.referral.save(update_fields=["gift", "gift_amount", "gift_status"])
        self.referral.refresh_from_db()

    def _decide(self, approved, comment="", token=None):
        return self.http.post(
            f"/api/referrals/{self.referral.id}/decide-gift/",
            data={"approved": approved, "comment": comment},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token or self.dist_token}",
        )

    def test_threshold_puts_gift_on_hold_not_issued(self):
        self._reach_threshold()

        self.assertTrue(self.referral.condition_met)
        self.assertEqual(self.referral.gift_status, "pending")
        self.assertFalse(self.referral.gift_is_issued)

    def test_client_does_not_see_gift_before_approval(self):
        """Обещать скидку до согласования нельзя — это деньги дистрибьютора."""
        self._reach_threshold()

        response = self.http.get(
            "/api/referrals/", HTTP_AUTHORIZATION=f"Bearer {self.inviter_token}"
        )
        data = response.json()

        self.assertIsNone(data["results"][0]["gift"])
        self.assertEqual(data["results"][0]["giftStatus"], "pending")
        self.assertEqual(data["stats"]["giftCount"], 0)
        self.assertEqual(data["stats"]["pendingGiftCount"], 1)

    def test_approval_issues_the_gift(self):
        self._reach_threshold()
        response = self._decide(True)
        self.assertEqual(response.status_code, 200, response.content)

        self.referral.refresh_from_db()
        self.assertTrue(self.referral.gift_is_issued)
        self.assertIsNotNone(self.referral.gift_decided_at)

        data = self.http.get(
            "/api/referrals/", HTTP_AUTHORIZATION=f"Bearer {self.inviter_token}"
        ).json()
        self.assertEqual(data["results"][0]["gift"], "Отсрочка 14 дней")
        self.assertEqual(data["stats"]["giftCount"], 1)

    def test_decline_keeps_gift_hidden_and_records_reason(self):
        self._reach_threshold()
        self._decide(False, comment="Клиент уже на спеццене")

        self.referral.refresh_from_db()
        self.assertEqual(self.referral.gift_status, "declined")
        self.assertFalse(self.referral.gift_is_issued)
        self.assertEqual(self.referral.gift_comment, "Клиент уже на спеццене")

    def test_client_is_notified_about_the_decision(self):
        self._reach_threshold()
        self._decide(True)

        self.assertTrue(
            Notification.objects.filter(
                user=self.inviter.user, title="Подарок согласован"
            ).exists()
        )

    def test_decision_cannot_be_taken_twice(self):
        self._reach_threshold()
        self._decide(True)

        repeat = self._decide(False)

        self.assertEqual(repeat.status_code, 409)
        self.referral.refresh_from_db()
        self.assertEqual(self.referral.gift_status, "approved")

    def test_other_distributor_cannot_decide(self):
        other = Distributor.objects.create(
            name="Чужой", inn="9998887776", phone="2", email="o@e.co"
        )
        other_user = User.objects.create_user(username="other", password="pw")
        Profile.objects.update_or_create(user=other_user, defaults={"role": "distributor"})
        other.user = other_user
        other.save(update_fields=["user"])
        self._reach_threshold()

        response = self._decide(True, token=self._token(other_user, "other-token"))

        self.assertEqual(response.status_code, 403)

    def test_client_cannot_approve_own_gift(self):
        self._reach_threshold()

        response = self._decide(True, token=self.inviter_token)

        self.assertEqual(response.status_code, 403)

    def test_pending_list_shows_only_own_region(self):
        self._reach_threshold()

        response = self.http.get(
            "/api/referrals/pending-gifts/", HTTP_AUTHORIZATION=f"Bearer {self.dist_token}"
        )
        results = response.json()["results"]

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["inviterName"], "Пригласивший")
        # Согласующему нужно видеть, что именно он подтверждает.
        self.assertEqual(results[0]["proposedGift"], "Отсрочка 14 дней")


class ArchivedClientTests(_Base):
    """п. 6: «архивный» — отдельный статус, а не блокировка."""

    def test_archived_is_an_allowed_status(self):
        client = self._client_profile("+79001110001", "1000000001")
        client.status = "archived"
        client.full_clean()
        client.save()

        client.refresh_from_db()
        self.assertEqual(client.status, "archived")

    def test_all_five_statuses_from_tz_exist(self):
        codes = {code for code, _ in ClientProfile.STATUS_CHOICES}
        self.assertEqual(
            codes, {"new", "under_review", "active", "blocked", "archived"}
        )


class LearningMaterialTests(_Base):
    """п. 10: уроки, чек-листы, видео и вебинары."""

    def setUp(self):
        super().setUp()
        self.client_profile = self._client_profile("+79001110001", "1000000001")
        self.token = self._token(self.client_profile.user, "client-token")

    def _material(self, title, kind="lesson", status="published", category="Покраска"):
        return LearningMaterial.objects.create(
            title=title, kind=kind, status=status, category=category
        )

    def _fetch(self, query=""):
        return self.http.get(
            f"/api/learning-materials/{query}", HTTP_AUTHORIZATION=f"Bearer {self.token}"
        )

    def test_published_material_is_visible_to_client(self):
        self._material("Подготовка поверхности", kind="video")

        results = self._fetch().json()["results"]

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Подготовка поверхности")
        self.assertEqual(results[0]["kindLabel"], "Видео")

    def test_draft_is_hidden_from_client(self):
        """Материалами управляют из админки — черновик клиенту не показываем."""
        self._material("Черновик", status="draft")

        self.assertEqual(self._fetch().json()["results"], [])

    def test_can_filter_by_kind(self):
        self._material("Урок", kind="lesson")
        self._material("Вебинар", kind="webinar")

        results = self._fetch("?kind=webinar").json()["results"]

        self.assertEqual([item["title"] for item in results], ["Вебинар"])

    def test_all_five_kinds_from_tz_exist(self):
        kinds = {code for code, _ in LearningMaterial.KIND_CHOICES}
        self.assertEqual(
            kinds, {"lesson", "checklist", "video", "manual", "webinar"}
        )

    def test_anonymous_cannot_read_materials(self):
        self._material("Урок")

        self.assertEqual(self.http.get("/api/learning-materials/").status_code, 401)


class AutoDraftKnowledgeCardTests(_Base):
    """п. 9 шаг 5: черновик карточки формируется сам."""

    def setUp(self):
        super().setUp()
        self.client_profile = self._client_profile("+79001110001", "1000000001")
        expert = User.objects.create_user(username="expert", password="pw")
        Profile.objects.update_or_create(user=expert, defaults={"role": "ai_expert"})
        self.expert_token = self._token(expert, "expert-token")

    def _ticket(self, category="Шагрень"):
        return ExpertTicket.objects.create(
            client=self.client_profile, question="Почему шагрень?", category=category
        )

    def _answer(self, ticket, **extra):
        payload = {"answer": "Снизить вязкость и поднять давление."}
        payload.update(extra)
        return self.http.post(
            f"/api/tickets/{ticket.id}/expert-answer/",
            data=payload,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.expert_token}",
        )

    def test_answer_creates_draft_card_without_asking(self):
        ticket = self._ticket()

        self._answer(ticket)

        card = KnowledgeCard.objects.filter(source_ticket=ticket).first()
        self.assertIsNotNone(card)
        self.assertEqual(card.status, "draft")

    def test_draft_is_never_published_automatically(self):
        """Правило безопасности п. 9: без утверждения AI карточку не использует."""
        ticket = self._ticket()

        self._answer(ticket)

        self.assertFalse(KnowledgeCard.objects.filter(status="approved").exists())

    def test_expert_can_opt_out(self):
        # Разовый вопрос знанием не станет — эксперт вправе отказаться.
        ticket = self._ticket()

        self._answer(ticket, createKnowledgeCard=False)

        self.assertFalse(KnowledgeCard.objects.filter(source_ticket=ticket).exists())

    def test_no_draft_without_category(self):
        # Без темы карточка не найдётся поиском и будет мусором в базе.
        ticket = self._ticket(category="")

        self._answer(ticket)

        self.assertFalse(KnowledgeCard.objects.filter(source_ticket=ticket).exists())


class GiftNotificationFromAnywhereTests(_Base):
    """Уведомление о решении не должно зависеть от способа согласования.

    Дистрибьютор может нажать кнопку в приложении, а может отметить запись в
    админке. Клиенту в обоих случаях нужно сообщить.
    """

    def setUp(self):
        super().setUp()
        self.inviter = self._client_profile("+79001110000", "5556667778", "Пригласивший")
        self.referral = Referral.objects.create(
            inviter=self.inviter, invitee_inn="7778889990",
            invitee_name="Приглашённый", region=self.region.name,
            condition_met=True, gift="Сертификат на 5000 ₽", gift_status="pending",
        )

    def _notifications(self, title):
        return Notification.objects.filter(user=self.inviter.user, title=title)

    def test_approving_by_plain_save_notifies_client(self):
        """Так подарок согласуют из админки — без обращения к API."""
        self.referral.gift_status = "approved"
        self.referral.save(update_fields=["gift_status"])

        self.assertEqual(self._notifications("Подарок согласован").count(), 1)

    def test_declining_by_plain_save_notifies_client(self):
        self.referral.gift_status = "declined"
        self.referral.save(update_fields=["gift_status"])

        self.assertEqual(self._notifications("Подарок не согласован").count(), 1)

    def test_repeated_save_does_not_duplicate_notification(self):
        self.referral.gift_status = "approved"
        self.referral.save(update_fields=["gift_status"])
        self.referral.gift_comment = "уточнение"
        self.referral.save(update_fields=["gift_comment"])

        self.assertEqual(self._notifications("Подарок согласован").count(), 1)

    def test_pending_alone_sends_no_decision_notice(self):
        # Условие выполнено — это ещё не решение по подарку.
        referral = Referral.objects.create(
            inviter=self.inviter, invitee_inn="1231231231",
            invitee_name="Второй", region=self.region.name,
        )
        referral.gift_status = "pending"
        referral.save(update_fields=["gift_status"])

        self.assertFalse(self._notifications("Подарок согласован").exists())
        self.assertFalse(self._notifications("Подарок не согласован").exists())
