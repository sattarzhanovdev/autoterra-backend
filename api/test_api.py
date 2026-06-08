import json
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from .models import Region, ClientProfile, Distributor, Purchase, AuthToken

class ApiRegistrationAndPurchaseTests(TestCase):
    def setUp(self):
        self.client = Client()
        # Создаем необходимые данные для тестов
        self.distributor_user = User.objects.create_user(username="dist_user", password="password")
        self.distributor = Distributor.objects.create(
            user=self.distributor_user,
            name="Test Distributor",
            inn="7701000001",
            phone="+79991112233",
            email="dist@example.com"
        )
        self.region = Region.objects.create(
            code="77",
            name="Москва",
            distributor=self.distributor
        )
        
    def test_registration_success(self):
        """Тест регистрации: валидные данные -> статус 201"""
        data = {
            "username": "+79001112233",
            "password": "Password123",
            "inn": "1234567890",
            "region_id": self.region.id,
            "company_name": "ООО АвтоТест",
            "contact_name": "Иван Иванов",
            "store_address": "Москва, Тестовая 1",
        }
        response = self.client.post(
            "/api/auth/register/", 
            data=json.dumps(data), 
            content_type="application/json"
        )
        
        self.assertEqual(response.status_code, 201, response.content.decode())
        res_data = response.json()
        self.assertEqual(res_data["status"], "success")
        self.assertTrue("token" in res_data)
        
        # Проверяем, что пользователь и профиль созданы
        self.assertTrue(User.objects.filter(username="+79001112233").exists())
        self.assertTrue(ClientProfile.objects.filter(inn="1234567890").exists())

    def test_regions_are_public_for_registration(self):
        """Тест: мобильная регистрация может загрузить регионы без токена."""
        response = self.client.get("/api/regions/")

        self.assertEqual(response.status_code, 200)
        res_data = response.json()
        self.assertEqual(len(res_data["results"]), 1)
        self.assertEqual(res_data["results"][0]["id"], str(self.region.id))
        self.assertEqual(res_data["results"][0]["name"], "Москва")

    def test_password_reset_updates_client_password(self):
        """Тест: клиент может сбросить пароль по телефону и ИНН."""
        user = User.objects.create_user(username="+79005556677", password="OldPass123")
        ClientProfile.objects.create(
            user=user,
            inn="1234567890",
            company_name="Password Reset Client",
            region=self.region,
            distributor=self.distributor,
            phone="+79005556677",
            city="Москва",
            contact_name="Reset Contact"
        )

        response = self.client.post(
            "/api/auth/password-reset/",
            data=json.dumps({
                "phone": "+79005556677",
                "inn": "1234567890",
                "new_password": "NewPass123",
            }),
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 200, response.content.decode())

        old_login = self.client.post(
            "/api/login/",
            data=json.dumps({"phone": "+79005556677", "password": "OldPass123"}),
            content_type="application/json"
        )
        self.assertEqual(old_login.status_code, 401)

        new_login = self.client.post(
            "/api/login/",
            data=json.dumps({"phone": "+79005556677", "password": "NewPass123"}),
            content_type="application/json"
        )
        self.assertEqual(new_login.status_code, 200, new_login.content.decode())

    def test_purchase_duplicate_antifraud(self):
        """Тест на антифрод/дубликаты при создании покупки"""
        # 1. Создаем пользователя и токен для авторизации
        user = User.objects.create_user(username="+79998887766", password="password")
        token = AuthToken.objects.create(key="test-token", user=user)
        client_profile = ClientProfile.objects.create(
            user=user,
            inn="9998887766",
            company_name="Purchase Client",
            region=self.region,
            distributor=self.distributor,
            phone="+79998887766",
            city="Москва",
            contact_name="Purchase Contact"
        )
        
        auth_headers = {"HTTP_AUTHORIZATION": f"Bearer {token.key}"}
        
        purchase_data = {
            "document_number": "INV-100",
            "date": "2026-06-01",
            "amount": "15500.50",
            "items": [
                {"sku": "P-01", "name": "Краска", "quantity": 1, "price": "15500.50"}
            ]
        }
        
        # 2. Успешное создание первой покупки
        response1 = self.client.post(
            "/api/purchases/create/", 
            data=json.dumps(purchase_data), 
            content_type="application/json",
            **auth_headers
        )
        self.assertEqual(response1.status_code, 201)
        self.assertEqual(Purchase.objects.count(), 1)
        
        # 3. Повторный запрос с теми же данными (дубликат)
        response2 = self.client.post(
            "/api/purchases/create/", 
            data=json.dumps(purchase_data), 
            content_type="application/json",
            **auth_headers
        )
        
        # Ожидаем ошибку 400 и специфичный текст
        self.assertEqual(response2.status_code, 400)
        res_data2 = response2.json()
        self.assertEqual(res_data2["error"], "duplicate_detected")
        self.assertIn("Покупка с такими данными уже загружена", res_data2["message"])
        
        # Проверяем, что вторая запись НЕ создалась
        self.assertEqual(Purchase.objects.count(), 1)
