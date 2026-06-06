import json
from django.test import TestCase, Client
from django.contrib.auth.models import User
from .models import Purchase, ClientProfile, Region, Distributor, AuthToken, PurchaseItem

class DistributorApiTests(TestCase):
    def setUp(self):
        self.client = Client()
        
        # 1. Среда
        self.dist_user = User.objects.create_user(username="+79001110001", password="password")
        self.distributor = Distributor.objects.create(
            user=self.dist_user, name="Main Dist", inn="123", phone="123", email="d@e.co"
        )
        self.region = Region.objects.create(code="77", name="Москва", distributor=self.distributor)
        self.token = AuthToken.objects.create(key="dist-token", user=self.dist_user)
        
        # 2. Клиент
        self.client_user = User.objects.create_user(username="client_user")
        self.client_profile = ClientProfile.objects.create(
            user=self.client_user,
            inn="1234567890", 
            company_name="Test Client", 
            region=self.region, 
            distributor=self.distributor, 
            phone="111", 
            city="Москва",
            contact_name="Contact"
        )
        
        # 3. Покупка (ожидает проверки)
        self.purchase = Purchase.objects.create(
            client=self.client_profile,
            distributor=self.distributor,
            document_number="INV-001",
            date="2026-06-06",
            total_amount=1000.0,
            status="pending"
        )
        PurchaseItem.objects.create(
            purchase=self.purchase, sku="S1", name="Product", quantity=10, price=100.0
        )

    def test_distributor_reject_purchase_with_reason(self):
        """Проверка: Дистрибьютор отклоняет покупку с указанием причины"""
        auth_headers = {"HTTP_AUTHORIZATION": f"Bearer {self.token.key}"}
        
        data = {
            "status": "rejected",
            "rejection_reason": "Нечитаемое фото чека"
        }
        
        url = f"/api/distributor/purchases/{self.purchase.id}/verify/"
        
        response = self.client.patch(
            url, 
            data=json.dumps(data), 
            content_type="application/json",
            **auth_headers
        )
        
        self.assertEqual(response.status_code, 200)
        
        # Проверяем базу данных
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.status, "rejected")
        self.assertEqual(self.purchase.rejection_reason, "Нечитаемое фото чека")
        
        # Проверяем ответ API
        res_data = response.json()
        self.assertEqual(res_data["purchase"]["status"], "rejected")
        self.assertEqual(res_data["purchase"]["rejectionReason"], "Нечитаемое фото чека")

    def test_distributor_cannot_verify_other_distributor_purchase(self):
        """Проверка: Дистрибьютор не видит/не может править покупки не своего региона"""
        other_user = User.objects.create_user(username="+79001110002", password="password")
        other_dist = Distributor.objects.create(user=other_user, name="Other", inn="456", phone="456", email="o@e.co")
        other_token = AuthToken.objects.create(key="other-token", user=other_user)
        
        auth_headers = {"HTTP_AUTHORIZATION": f"Bearer {other_token.key}"}
        url = f"/api/distributor/purchases/{self.purchase.id}/verify/"
        
        response = self.client.patch(
            url, 
            data=json.dumps({"status": "verified"}), 
            content_type="application/json",
            **auth_headers
        )
        
        self.assertEqual(response.status_code, 404) # Не найден в его области видимости
