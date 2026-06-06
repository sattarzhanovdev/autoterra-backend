from django.test import TestCase
from django.contrib.auth.models import User
from .models import KnowledgeCard

class KnowledgeBaseTests(TestCase):
    def setUp(self):
        self.expert_user = User.objects.create_user(username="expert_tech", password="password")

    def test_knowledge_card_default_status_is_draft(self):
        """Проверка: новая карточка базы знаний всегда создается со статусом 'draft'"""
        card = KnowledgeCard.objects.create(
            problem="Пузырение лака",
            solution="Снизить температуру сушки",
            expert_author=self.expert_user
        )
        
        # Проверяем дефолтное значение в БД
        self.assertEqual(card.status, "draft")
        self.assertEqual(card.get_status_display(), "Черновик")

    def test_knowledge_card_approval_workflow(self):
        """Проверка: переход статуса из draft в approved"""
        card = KnowledgeCard.objects.create(
            problem="Кратер на базе",
            solution="Обезжирить поверхность антисиликоном",
            expert_author=self.expert_user
        )
        
        self.assertEqual(card.status, "draft")
        
        # Имитируем одобрение экспертом
        card.status = "approved"
        card.save()
        
        # Проверяем сохранение
        card.refresh_from_db()
        self.assertEqual(card.status, "approved")
        self.assertEqual(card.get_status_display(), "Одобрено")
