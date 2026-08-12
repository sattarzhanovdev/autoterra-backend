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
