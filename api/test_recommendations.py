"""Персональные AI-рекомендации (п. 13 ТЗ).

ТЗ: «Система должна не просто отправлять одинаковые напоминания, а формировать
персональные подсказки на основе поведения клиента, покупок, статуса, региона и
истории обращений». Проверяем, что подсказки действительно зависят от данных
клиента и не повторяются каждый день.
"""

from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from .models import (
    ClientProfile,
    Distributor,
    ExpertTicket,
    KnowledgeCard,
    ManagerTask,
    Notification,
    Order,
    OrderItem,
    Product,
    Purchase,
    PurchaseItem,
    Region,
)
from .services import recommendations as rec


class RecommendationTestBase(TestCase):
    def setUp(self):
        patcher = patch(
            "api.services.push_notifications.PushNotificationService.send",
            return_value={"sent": 0, "failed": 0},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.distributor = Distributor.objects.create(
            name="Дист", inn="1112223334", phone="1", email="d@e.co"
        )
        self.manager = User.objects.create_user(username="manager", password="pw")
        self.region = Region.objects.create(
            code="77", name="Москва", distributor=self.distributor, manager=self.manager
        )
        self.product = Product.objects.create(
            distributor=self.distributor, sku="P-1", name="Краска базовая",
            category="Краски", price=Decimal(1000), quantity=1000,
        )
        self._inn = 1000000000

    def make_client(self, username, status="active", created_days_ago=200):
        self._inn += 1
        user = User.objects.create_user(username=username, password="pw")
        client = ClientProfile.objects.create(
            user=user, inn=str(self._inn), company_name=f"Компания {username}",
            contact_name="Иван", phone=username, status=status,
            region=self.region, city="Москва", distributor=self.distributor,
        )
        # created_at стоит auto_now_add — правим точечно.
        ClientProfile.objects.filter(pk=client.pk).update(
            created_at=timezone.now() - timedelta(days=created_days_ago)
        )
        client.refresh_from_db()
        return client

    def add_purchase(self, client, days_ago, amount=10_000, sku="P-1", name="Краска базовая"):
        purchase = Purchase.objects.create(
            client=client, distributor=self.distributor,
            document_number=f"D{client.pk}-{days_ago}-{sku}",
            date=timezone.localdate() - timedelta(days=days_ago),
            total_amount=Decimal(amount), status="verified",
        )
        PurchaseItem.objects.create(
            purchase=purchase, sku=sku, name=name, category="Краски", quantity=1,
            price=Decimal(amount),
        )
        return purchase

    def titles_for(self, client):
        return list(
            Notification.objects.filter(user=client.user).values_list("title", flat=True)
        )


class DormantClientTests(RecommendationTestBase):
    """«Давно не было покупки»."""

    def test_silent_client_gets_reminder(self):
        client = self.make_client("+79001110001")
        self.add_purchase(client, days_ago=60)

        items = rec.dormant_clients(days=45)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].user, client.user)
        self.assertEqual(items[0].title, "Давно не было заказа")
        self.assertIn("60 дн. назад", items[0].body)

    def test_recent_buyer_is_left_alone(self):
        client = self.make_client("+79001110001")
        self.add_purchase(client, days_ago=5)

        self.assertEqual(rec.dormant_clients(days=45), [])

    def test_paid_order_counts_as_activity(self):
        client = self.make_client("+79001110001")
        order = Order.objects.create(
            client=client, distributor=self.distributor, status="paid"
        )
        OrderItem.objects.create(
            order=order, product=self.product, sku="P-1", name="Краска",
            price=Decimal(5000), quantity=1,
        )

        self.assertEqual(rec.dormant_clients(days=45), [])

    def test_never_bought_old_client_gets_different_text(self):
        client = self.make_client("+79001110001", created_days_ago=90)

        items = rec.dormant_clients(days=45)

        self.assertEqual(len(items), 1)
        self.assertIn("ещё не оформляли заказ", items[0].body)

    def test_brand_new_client_is_not_nagged(self):
        self.make_client("+79001110001", created_days_ago=3)
        self.assertEqual(rec.dormant_clients(days=45), [])

    def test_blocked_client_is_skipped(self):
        client = self.make_client("+79001110001", status="blocked")
        self.add_purchase(client, days_ago=90)

        self.assertEqual(rec.dormant_clients(days=45), [])


class RepeatSkuTests(RecommendationTestBase):
    """«Заканчиваются типовые SKU»."""

    def test_regular_sku_triggers_reminder(self):
        client = self.make_client("+79001110001")
        self.add_purchase(client, days_ago=90, sku="P-1", name="Краска базовая")
        self.add_purchase(client, days_ago=60, sku="P-1", name="Краска базовая")

        items = rec.repeat_sku(days=30)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Пора пополнить запас")
        self.assertIn("Краска базовая", items[0].body)
        self.assertEqual(items[0].meta["skus"], ["P-1"])

    def test_single_purchase_is_not_a_pattern(self):
        client = self.make_client("+79001110001")
        self.add_purchase(client, days_ago=60, sku="P-1")

        self.assertEqual(rec.repeat_sku(days=30), [])

    def test_recently_restocked_sku_is_skipped(self):
        client = self.make_client("+79001110001")
        self.add_purchase(client, days_ago=90, sku="P-1")
        self.add_purchase(client, days_ago=5, sku="P-1")

        self.assertEqual(rec.repeat_sku(days=30), [])

    def test_suggestion_is_personal_not_generic(self):
        """Двум клиентам с разными покупками уходят разные подсказки."""
        first = self.make_client("+79001110001")
        self.add_purchase(first, days_ago=90, sku="P-1", name="Краска базовая")
        self.add_purchase(first, days_ago=60, sku="P-1", name="Краска базовая")

        second = self.make_client("+79001110002")
        self.add_purchase(second, days_ago=90, sku="L-9", name="Лак акриловый")
        self.add_purchase(second, days_ago=60, sku="L-9", name="Лак акриловый")

        items = {item.user: item.body for item in rec.repeat_sku(days=30)}

        self.assertIn("Краска базовая", items[first.user])
        self.assertNotIn("Лак акриловый", items[first.user])
        self.assertIn("Лак акриловый", items[second.user])


class FrequentDefectTests(RecommendationTestBase):
    """«Частый дефект»."""

    def _ticket(self, client, category):
        return ExpertTicket.objects.create(
            client=client, question=f"Проблема с {category}", category=category
        )

    def test_repeated_topic_triggers_hint(self):
        client = self.make_client("+79001110001")
        self._ticket(client, "Шагрень")
        self._ticket(client, "Шагрень")

        items = rec.frequent_defects(min_tickets=2)

        self.assertEqual(len(items), 1)
        self.assertIn("Шагрень", items[0].title)
        self.assertIn("обращались 2 раза", items[0].body)

    def test_single_ticket_is_not_frequent(self):
        client = self.make_client("+79001110001")
        self._ticket(client, "Шагрень")

        self.assertEqual(rec.frequent_defects(min_tickets=2), [])

    def test_knowledge_card_is_offered_when_available(self):
        client = self.make_client("+79001110001")
        self._ticket(client, "Шагрень")
        self._ticket(client, "Шагрень")
        KnowledgeCard.objects.create(
            title="Шагрень при покраске", category="Шагрень",
            problem="Шагрень", solution="Снизить давление", status="approved",
        )

        body = rec.frequent_defects(min_tickets=2)[0].body
        self.assertIn("Шагрень при покраске", body)

    def test_without_card_offers_expert(self):
        client = self.make_client("+79001110001")
        self._ticket(client, "Подтёки")
        self._ticket(client, "Подтёки")

        body = rec.frequent_defects(min_tickets=2)[0].body
        self.assertIn("консультацию эксперта", body)


class NewMaterialTests(RecommendationTestBase):
    """«Новый обучающий материал» — только по темам клиента."""

    def test_material_matching_client_topic(self):
        client = self.make_client("+79001110001")
        ExpertTicket.objects.create(client=client, question="?", category="Полировка")
        KnowledgeCard.objects.create(
            title="Полировка кузова", category="Полировка",
            problem="Полировка", solution="...", status="approved",
        )

        items = rec.new_materials(days=14)

        self.assertEqual(len(items), 1)
        self.assertIn("Полировка кузова", items[0].body)

    def test_material_on_foreign_topic_is_not_pushed(self):
        client = self.make_client("+79001110001")
        ExpertTicket.objects.create(client=client, question="?", category="Полировка")
        KnowledgeCard.objects.create(
            title="Сварка", category="Сварка", problem="Сварка",
            solution="...", status="approved",
        )

        self.assertEqual(rec.new_materials(days=14), [])

    def test_draft_card_is_not_announced(self):
        client = self.make_client("+79001110001")
        ExpertTicket.objects.create(client=client, question="?", category="Полировка")
        KnowledgeCard.objects.create(
            title="Черновик", category="Полировка", problem="?",
            solution="...", status="draft",
        )

        self.assertEqual(rec.new_materials(days=14), [])

    def test_old_card_is_not_new(self):
        client = self.make_client("+79001110001")
        ExpertTicket.objects.create(client=client, question="?", category="Полировка")
        card = KnowledgeCard.objects.create(
            title="Старое", category="Полировка", problem="?",
            solution="...", status="approved",
        )
        KnowledgeCard.objects.filter(pk=card.pk).update(
            created_at=timezone.now() - timedelta(days=60)
        )

        self.assertEqual(rec.new_materials(days=14), [])


class LowRegionActivityTests(RecommendationTestBase):
    """«Низкая активность региона» → задача менеджеру, а не push клиенту."""

    def test_silent_region_creates_manager_task(self):
        self.make_client("+79001110001")

        tasks = rec.low_activity_regions(days=30)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].manager, self.manager)
        self.assertIn("Москва", tasks[0].text)
        self.assertIsNotNone(tasks[0].deadline)

    def test_active_region_creates_nothing(self):
        client = self.make_client("+79001110001")
        self.add_purchase(client, days_ago=2)

        self.assertEqual(rec.low_activity_regions(days=30), [])

    def test_empty_region_is_ignored(self):
        # Клиентов нет — развивать пока нечего.
        self.assertEqual(rec.low_activity_regions(days=30), [])

    def test_task_is_not_duplicated_while_pending(self):
        self.make_client("+79001110001")
        rec.low_activity_regions(days=30)

        rec.low_activity_regions(days=30)

        self.assertEqual(ManagerTask.objects.count(), 1)

    def test_region_without_manager_is_skipped(self):
        Region.objects.filter(pk=self.region.pk).update(manager=None)
        self.make_client("+79001110001")

        self.assertEqual(rec.low_activity_regions(days=30), [])


class SendingTests(RecommendationTestBase):
    """Отправка и защита от повторов."""

    def _dormant_client(self):
        client = self.make_client("+79001110001")
        self.add_purchase(client, days_ago=60)
        return client

    def test_send_creates_notification_and_push(self):
        client = self._dormant_client()

        stats = rec.send(rec.dormant_clients(days=45))

        self.assertEqual(stats["sent"], 1)
        self.assertEqual(self.titles_for(client), ["Давно не было заказа"])
        note = Notification.objects.get(user=client.user)
        self.assertEqual(note.type, "recommendation")
        self.assertEqual(note.related_link, "/order")

    def test_same_hint_is_not_repeated_within_cooldown(self):
        client = self._dormant_client()

        rec.send(rec.dormant_clients(days=45), cooldown_days=14)
        stats = rec.send(rec.dormant_clients(days=45), cooldown_days=14)

        self.assertEqual(stats["sent"], 0)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(len(self.titles_for(client)), 1)

    def test_hint_repeats_after_cooldown_passes(self):
        client = self._dormant_client()
        rec.send(rec.dormant_clients(days=45))
        Notification.objects.filter(user=client.user).update(
            created_at=timezone.now() - timedelta(days=30)
        )

        stats = rec.send(rec.dormant_clients(days=45), cooldown_days=14)

        self.assertEqual(stats["sent"], 1)
        self.assertEqual(len(self.titles_for(client)), 2)

    def test_dry_run_sends_nothing(self):
        client = self._dormant_client()

        stats = rec.send(rec.dormant_clients(days=45), dry_run=True)

        self.assertEqual(stats["sent"], 1)
        self.assertEqual(self.titles_for(client), [])

    def test_broken_rule_does_not_stop_the_rest(self):
        self._dormant_client()

        with patch.object(rec, "repeat_sku", side_effect=RuntimeError("сломалось")):
            items = rec.collect()

        self.assertTrue(any(item.rule == "dormant" for item in items))

    def test_push_failure_does_not_lose_notification(self):
        client = self._dormant_client()

        with patch(
            "api.services.push_notifications.PushNotificationService.send",
            side_effect=RuntimeError("FCM недоступен"),
        ):
            rec.send(rec.dormant_clients(days=45))

        # Пуш не ушёл, но в колокольчике уведомление есть.
        self.assertEqual(self.titles_for(client), ["Давно не было заказа"])


class CommandTests(RecommendationTestBase):
    def setUp(self):
        super().setUp()
        client = self.make_client("+79001110001")
        self.add_purchase(client, days_ago=60)
        self.client_profile = client

    def _run(self, *args):
        out = StringIO()
        call_command("send_recommendations", *args, stdout=out, stderr=StringIO())
        return out.getvalue()

    def test_dry_run_reports_without_sending(self):
        output = self._run()

        self.assertIn("Пробный прогон", output)
        self.assertIn("dormant: 1", output)
        self.assertEqual(Notification.objects.count(), 0)

    def test_apply_sends(self):
        output = self._run("--apply")

        self.assertIn("Отправлено: 1", output)
        self.assertEqual(Notification.objects.count(), 1)

    def test_rules_filter(self):
        output = self._run("--apply", "--rules", "repeat_sku")

        self.assertIn("Подсказок сформировано: 0", output)
        self.assertEqual(Notification.objects.count(), 0)

    def test_unknown_rule_is_rejected(self):
        out, err = StringIO(), StringIO()
        call_command("send_recommendations", "--rules", "nosuchrule", stdout=out, stderr=err)

        self.assertIn("Неизвестные правила", err.getvalue())
        self.assertEqual(Notification.objects.count(), 0)

    def test_region_tasks_can_be_skipped(self):
        self._run("--apply", "--skip-region-tasks")
        self.assertEqual(ManagerTask.objects.count(), 0)
