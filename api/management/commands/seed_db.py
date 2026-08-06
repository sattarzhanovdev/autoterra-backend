import os
import secrets
from datetime import datetime, timedelta
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.utils import timezone
from api.models import (
    Profile, ClientProfile, Distributor, Region, Purchase, PurchaseItem,
    Order, OrderItem, ColorRequest, CourierTask, ExpertTicket, 
    KnowledgeCard, Notification, Product, Store, AuditLog, Referral
)

class Command(BaseCommand):
    help = "Seeds the database for E2E testing and generates a system report."

    def handle(self, *args, **options):
        self.stdout.write("Wiping existing data...")
        # 1. Wipe Data
        models_to_wipe = [
            Purchase, Order, ColorRequest, CourierTask, ExpertTicket, 
            KnowledgeCard, Notification, ClientProfile, Region, 
            Distributor, Product, Store, AuditLog, Referral, Profile
        ]
        for model in models_to_wipe:
            model.objects.all().delete()
        
        # Keep superusers
        User.objects.filter(is_superuser=False).delete()

        password = "TestPass123!"
        self.stdout.write("Creating roles and regions...")

        # 2. Seed Regions
        region_msk = Region.objects.create(code="77", name="Москва - Центральный")
        region_kzn = Region.objects.create(code="16", name="Казань - Поволжье")

        # 3. Create Users & Profiles
        def create_user(username, role, region=None, is_staff=False):
            user = User.objects.create_user(username=username, password=password)
            if is_staff:
                # Без is_staff в Django-админку не пускают вообще, поэтому
                # менеджер не мог открыть аналитику закупок.
                user.is_staff = True
                user.save(update_fields=["is_staff"])
            # Profile is created by signal, so we just update it
            profile, _ = Profile.objects.get_or_create(user=user)
            profile.role = role
            profile.save()
            return user

        # Distributors
        u_dist_msk = create_user("distributor_msk", "distributor")
        dist_msk = Distributor.objects.create(
            user=u_dist_msk, name="AutoTerra МСК", inn="7700000010", 
            phone="+79001112233", email="msk@autoterra.ru"
        )
        region_msk.distributor = dist_msk
        region_msk.save()

        u_dist_kzn = create_user("distributor_kzn", "distributor")
        dist_kzn = Distributor.objects.create(
            user=u_dist_kzn, name="AutoTerra КЗН", inn="1600000010", 
            phone="+79002223344", email="kzn@autoterra.ru"
        )
        region_kzn.distributor = dist_kzn
        region_kzn.save()

        # Manager (Admin / Central Office)
        u_manager = create_user("manager_main", "admin", is_staff=True)

        # Courier
        u_courier = create_user("courier_msk", "courier")

        # Expert
        u_expert = create_user("expert_ai", "expert")

        # Clients
        def create_client(username, company, region, distributor, inn):
            user = create_user(username, "client")
            client = ClientProfile.objects.create(
                user=user, inn=inn, company_name=company,
                region=region, distributor=distributor,
                city=region.name.split(" ")[0], contact_name=f"Мастер {username}",
                phone=f"+7900{secrets.token_hex(4)}"[:12]
            )
            return client

        c1_msk = create_client("client1_msk", "МСК Тюнинг Лаб", region_msk, dist_msk, "7712345678")
        c2_msk = create_client("client2_msk", "Премиум Покраска МСК", region_msk, dist_msk, "7787654321")
        c3_kzn = create_client("client3_kzn", "Казань СТО", region_kzn, dist_kzn, "1612345678")

        # 4. Seed Business Data
        self.stdout.write("Generating business data...")
        
        # Products
        categories = ["Лак", "Грунт", "Разбавитель"]
        for dist in [dist_msk, dist_kzn]:
            Product.objects.create(
                distributor=dist, sku=f"LAK-{dist.id}-01", name="Лак Ultra Gloss", 
                category="Лак", brand="AutoTerra", price=2500, quantity=50, status="inStock"
            )
            Product.objects.create(
                distributor=dist, sku=f"GRN-{dist.id}-02", name="Грунт-изолятор", 
                category="Грунт", brand="AutoTerra", price=1800, quantity=3, status="low"
            )
            Product.objects.create(
                distributor=dist, sku=f"RZB-{dist.id}-03", name="Разбавитель акриловый", 
                category="Разбавитель", brand="AutoTerra", price=900, quantity=0, status="onOrder"
            )

        # Stores
        Store.objects.create(client=c1_msk, name="Главный Склад МСК", address="г. Москва, ул. Ленина 1")
        Store.objects.create(client=c3_kzn, name="Пункт выдачи КЗН", address="г. Казань, ул. Баумана 5")
        store_msk = Store.objects.filter(client=c1_msk).first()

        # Purchases
        clients = [c1_msk, c2_msk, c3_kzn]
        for client in clients:
            # Verified
            p1 = Purchase.objects.create(
                client=client, distributor=client.distributor, 
                document_number=f"INV-{secrets.token_hex(4).upper()}", 
                date=timezone.now().date(), total_amount=15000, status="verified"
            )
            PurchaseItem.objects.create(purchase=p1, sku="LAK-1", name="Лак", quantity=2, price=7500)
            
            # Under Review
            p2 = Purchase.objects.create(
                client=client, distributor=client.distributor, 
                document_number=f"INV-{secrets.token_hex(4).upper()}", 
                date=timezone.now().date(), total_amount=5000, status="new"
            )
            PurchaseItem.objects.create(purchase=p2, sku="GRN-1", name="Грунт", quantity=1, price=5000)

        # Orders
        Order.objects.create(client=c1_msk, store=store_msk, distributor=dist_msk, status="new")
        Order.objects.create(client=c3_kzn, store=Store.objects.filter(client=c3_kzn).first(), distributor=dist_kzn, status="accepted")

        # Color Lab
        # 1. Self Delivery
        ColorRequest.objects.create(
            client=c1_msk, car_brand="BMW", car_model="X5", vin="VIN123MSK", 
            color_code="300", transfer_method="self_delivery", status="created"
        )
        # 2. Courier (This should trigger Signal)
        ColorRequest.objects.create(
            client=c1_msk, car_brand="Audi", car_model="A6", vin="VIN456MSK", 
            color_code="LY9B", transfer_method="courier", 
            pickup_address="Москва, Сервис Центр 1", pickup_time=timezone.now() + timedelta(hours=3),
            status="created"
        )

        # Extra Courier Tasks
        CourierTask.objects.create(
            client=c2_msk, task_type="delivery", address="Москва, ул. Тверская 10", 
            time_slot="10:00-14:00", status="assigned", courier=u_courier
        )
        CourierTask.objects.create(
            client=c1_msk, task_type="pickup", address="Москва, пр. Мира 22", 
            time_slot="14:00-18:00", status="in_progress", courier=u_courier
        )

        # AI & Experts
        ExpertTicket.objects.create(
            client=c1_msk, question="Как правильно разводить лак Ultra Gloss?", 
            category="Технология", status="open"
        )
        ExpertTicket.objects.create(
            client=c2_msk, question="Подойдет ли грунт-изолятор для пластика?", 
            category="Подбор материала", status="aiAnswered"
        )
        KnowledgeCard.objects.create(
            problem="Кратеры на лаке", solution="Проверить чистоту воздуха в компрессоре", 
            status="approved", expert_author=u_expert
        )
        KnowledgeCard.objects.create(
            problem="Шагрень при покраске", solution="Увеличить давление или разбавить краску", 
            status="draft", expert_author=u_expert
        )

        # Notifications
        Notification.objects.create(
            user=c1_msk.user, title="Подарок начислен!", 
            body="Ваш реферал совершил покупку. Вам начислен бонус!", type="referral"
        )
        Notification.objects.create(
            user=c3_kzn.user, title="Заказ принят", 
            body="Дистрибьютор в Казани принял ваш заказ в работу.", type="order"
        )

        self.stdout.write("Generating report...")
        # 5. Generate Report
        report_content = f"""# Системный отчет платформы AutoTerra

## Блок 1: Системный отчет платформы AutoTerra

**AutoTerra** — это комплексная B2B-экосистема для оптимизации взаимодействия между автосервисами (СТО), региональными дистрибьюторами и импортером. Платформа автоматизирует цепочку поставок, лабораторию цвета и техническую поддержку.

### Ключевые механизмы:
1. **RBAC и Региональность:** Система использует ролевую модель доступа (Client, Distributor, Manager, Courier, Expert). Доступ к данным строго ограничен регионом: дистрибьютор видит только клиентов своего региона, а клиенты привязаны к конкретному дистрибьютору.
2. **Процесс Покупок:** Клиенты загружают данные о покупках из сторонних магазинов для получения бонусов. Дистрибьютор проверяет эти чеки (статусы: `new` -> `verified`/`rejected`).
3. **Color Lab + Логистика:** При создании заявки на колеровку с выбором доставки курьером, система автоматически генерирует `CourierTask`. Это связывает лабораторию и службу доставки в единый процесс.
4. **AI-ассистент и Эксперты:** AI отвечает на вопросы на основе базы знаний (`KnowledgeCard`). Если AI не уверен или вопрос касается критических технологий, система предлагает эскалацию на Эксперта-технолога, создавая `ExpertTicket`.

## Блок 2: Тестовые доступы

У всех пользователей пароль: `{password}`

| Роль | Логин | Регион / Привязка |
| :--- | :--- | :--- |
| **Менеджер (Импортер)** | `manager_main` | Все регионы |
| **Дистрибьютор МСК** | `distributor_msk` | Москва - Центральный |
| **Дистрибьютор КЗН** | `distributor_kzn` | Казань - Поволжье |
| **Курьер МСК** | `courier_msk` | Москва |
| **AI Эксперт** | `expert_ai` | База знаний |
| **Клиент 1 (МСК)** | `client1_msk` | Москва (Дист: МСК) |
| **Клиент 2 (МСК)** | `client2_msk` | Москва (Дист: МСК) |
| **Клиент 3 (КЗН)** | `client3_kzn` | Казань (Дист: КЗН) |

## Блок 3: Карта данных и сценарии проверок

### 1. Сценарий: Дистрибьютор МСК (`distributor_msk`)
- **Что увидит:** Список клиентов (`МСК Тюнинг Лаб`, `Премиум Покраска МСК`).
- **Задачи:** У него есть 4 покупки на проверке (`new`) от московских клиентов. Нужно зайти в раздел "Заказы/Покупки" и подтвердить одну из них.

### 2. Сценарий: Клиент 1 МСК (`client1_msk`)
- **Что увидит:** 
    - 2 покупки (1 подтверждена, 1 на проверке).
    - 2 заявки Color Lab (одна "Сам привезу", одна "Курьер").
    - Уведомление о начисленном реферальном бонусе.
- **Сценарий:** Проверить статус заявки на колеровку и наличие рецептов.

### 3. Сценарий: Курьер МСК (`courier_msk`)
- **Что увидит:** 3 активные задачи.
    - 1 задача на забор лючка (автоматически создана из Color Lab для `client1_msk`).
    - 1 задача на доставку (`assigned`).
    - 1 задача на забор (`in_progress`).
- **Сценарий:** Принять задачу `assigned` и перевести в `delivered`.

### 4. Сценарий: AI Эксперт (`expert_ai`)
- **Что увидит:** Раздел базы знаний.
- **Задачи:** В списке есть одна карточка в статусе `draft` ("Шагрень при покраске"). Нужно перевести ее в `approved`, чтобы AI начал использовать этот совет.

### 5. Сценарий: Менеджер (`manager_main`)
- **Что увидит:** Глобальный дашборд.
- **Сценарий:** Проверить статистику по регионам (Москва vs Казань) и зайти в "Единую карту клиента" для `client1_msk`, чтобы увидеть всю его историю.

---
*Сгенерировано автоматически скриптом `seed_db`*
"""
        with open("autoterra_system_report.md", "w", encoding="utf-8") as f:
            f.write(report_content)

        self.stdout.write(self.style.SUCCESS("Database seeded successfully! Report generated at autoterra_system_report.md"))
