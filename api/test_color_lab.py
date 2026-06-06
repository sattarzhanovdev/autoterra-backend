from django.test import TestCase
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta
from .models import ColorRequest, ClientProfile, Region, Distributor, CourierTask

class ColorLabTests(TestCase):
    def setUp(self):
        self.dist_user = User.objects.create_user(username="dist_user", password="password")
        self.distributor = Distributor.objects.create(
            user=self.dist_user, name="Dist", inn="1", phone="1", email="d@e.co"
        )
        self.region = Region.objects.create(code="77", name="Msk", distributor=self.distributor)
        self.client_user = User.objects.create_user(username="client_user")
        self.client_profile = ClientProfile.objects.create(
            user=self.client_user,
            inn="1234567890",
            company_name="Client",
            region=self.region,
            distributor=self.distributor,
            phone="1", city="Msk", contact_name="Me"
        )

    def test_courier_task_auto_creation(self):
        """Проверка: при создании ColorRequest с методом 'courier' создается задача курьеру"""
        req = ColorRequest.objects.create(
            client=self.client_profile,
            car_brand="BMW",
            car_model="X5",
            vin="VIN123",
            color_code="300",
            transfer_method="courier",
            pickup_address="Pushkin st, 10",
            pickup_time=timezone.now() + timedelta(hours=2)
        )
        
        # Проверяем, что задача создана через сигнал
        task = CourierTask.objects.filter(color_request=req).first()
        self.assertIsNotNone(task)
        self.assertEqual(task.task_type, "color_lab_pickup")
        self.assertEqual(task.address, "Pushkin st, 10")
        self.assertEqual(task.client, self.client_profile)

    def test_sla_deadline_calculation(self):
        """Проверка: расчет SLA дедлайна (+24 часа по умолчанию)"""
        now = timezone.now()
        req = ColorRequest.objects.create(
            client=self.client_profile,
            car_brand="Audi",
            car_model="A4",
            vin="VIN456",
            color_code="LY9B",
            urgent=False
        )
        
        # Допускаем небольшую погрешность в несколько секунд
        expected_deadline = now + timedelta(hours=24)
        self.assertAlmostEqual(
            req.sla_deadline.timestamp(), 
            expected_deadline.timestamp(), 
            delta=5
        )

    def test_urgent_sla_deadline(self):
        """Проверка: расчет SLA дедлайна для срочных заявок (+4 часа)"""
        now = timezone.now()
        req = ColorRequest.objects.create(
            client=self.client_profile,
            car_brand="Porsche",
            car_model="911",
            vin="VIN911",
            color_code="G1",
            urgent=True
        )
        
        expected_deadline = now + timedelta(hours=4)
        self.assertAlmostEqual(
            req.sla_deadline.timestamp(), 
            expected_deadline.timestamp(), 
            delta=5
        )
