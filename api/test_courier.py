import json
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from .models import CourierTask, ClientProfile, Region, Distributor, AuthToken

class CourierApiTests(TestCase):
    def setUp(self):
        self.client = Client()
        
        # 1. Среда (Группы, Регионы, Дистрибьюторы)
        self.courier_group, _ = Group.objects.get_or_create(name='courier')
        self.distributor_user = User.objects.create_user(username="dist", password="password")
        self.distributor = Distributor.objects.create(
            user=self.distributor_user, name="Dist", inn="123", phone="123", email="d@e.co"
        )
        self.region = Region.objects.create(code="77", name="Msk", distributor=self.distributor)
        
        # 2. Курьер А
        self.courier_a = User.objects.create_user(username="+79001111111", password="password")
        self.courier_a.groups.add(self.courier_group)
        self.token_a = AuthToken.objects.create(key="token-a", user=self.courier_a)
        
        # 3. Курьер Б
        self.courier_b = User.objects.create_user(username="+79002222222", password="password")
        self.courier_b.groups.add(self.courier_group)
        self.token_b = AuthToken.objects.create(key="token-b", user=self.courier_b)
        
        # 4. Профиль клиента (для создания задач)
        self.client_profile = ClientProfile.objects.create(
            user=User.objects.create_user(username="client_user"),
            inn="1234567890", company_name="Client", region=self.region, 
            distributor=self.distributor, phone="111", city="Msk",
            contact_name="Test Contact"
        )
        
        # 5. Задачи
        self.task_a = CourierTask.objects.create(
            courier=self.courier_a,
            client=self.client_profile,
            task_type="delivery",
            address="Address A",
            time_slot="10:00-12:00",
            status="assigned"
        )
        
        self.task_b = CourierTask.objects.create(
            courier=self.courier_b,
            client=self.client_profile,
            task_type="pickup",
            address="Address B",
            time_slot="12:00-14:00",
            status="assigned"
        )

    def test_courier_can_only_see_their_tasks(self):
        """Проверка: Курьер А видит только свои задачи"""
        auth_headers = {"HTTP_AUTHORIZATION": f"Bearer {self.token_a.key}"}
        
        response = self.client.get("/api/courier/tasks/", **auth_headers)
        self.assertEqual(response.status_code, 200)
        
        res_data = response.json()
        tasks = res_data["results"]
        
        # Курьер А должен видеть только 1 задачу (свою)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["id"], str(self.task_a.id))
        self.assertEqual(tasks[0]["address"], "Address A")

    def test_courier_can_update_task_status(self):
        """Проверка: Успешное обновление статуса на in_progress"""
        auth_headers = {"HTTP_AUTHORIZATION": f"Bearer {self.token_a.key}"}
        
        data = {
            "status": "in_progress",
            "courier_comment": "Выехал на заказ"
        }
        
        url = f"/api/courier/tasks/{self.task_a.id}/status/"
        
        # Используем PATCH (или POST, так как в views разрешены оба)
        response = self.client.patch(
            url, 
            data=json.dumps(data), 
            content_type="application/json",
            **auth_headers
        )
        
        self.assertEqual(response.status_code, 200)
        
        # Проверяем изменения в базе
        self.task_a.refresh_from_db()
        self.assertEqual(self.task_a.status, "in_progress")
        self.assertEqual(self.task_a.courier_comment, "Выехал на заказ")
        
        # Проверяем, что в истории появился новый статус
        self.assertEqual(len(self.task_a.status_history), 1)
        self.assertEqual(self.task_a.status_history[0]["status"], "in_progress")

    def test_courier_cannot_update_others_task(self):
        """Проверка: Курьер А не может обновить задачу Курьера Б"""
        auth_headers = {"HTTP_AUTHORIZATION": f"Bearer {self.token_a.key}"}
        
        data = {"status": "in_progress"}
        url = f"/api/courier/tasks/{self.task_b.id}/status/"
        
        response = self.client.patch(
            url, 
            data=json.dumps(data), 
            content_type="application/json",
            **auth_headers
        )
        
        # Ожидаем 403 Forbidden
        self.assertEqual(response.status_code, 403)
        self.task_b.refresh_from_db()
        self.assertEqual(self.task_b.status, "assigned") # Статус не изменился
