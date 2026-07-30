"""Сквозная логика доставки заказа.

    заказ оплачен → создаётся доставка
    дистрибьютор назначает курьера → «Курьер назначен»
    курьер берётся за работу → «В пути»
    курьер завершает доставку → «Доставлено»
"""

from django.contrib.auth.models import User
from django.test import Client, TestCase

from .models import AuthToken, ClientProfile, CourierTask, Distributor, Order, Region, Store


class OrderDeliveryFlowTests(TestCase):
    def setUp(self):
        self.http = Client()
        self.distributor_user = User.objects.create_user(username="dist", password="pw")
        self.distributor_user.profile.role = "distributor"
        self.distributor_user.profile.save()
        self.distributor = Distributor.objects.create(
            user=self.distributor_user, name="Dist", inn="1112223334", phone="1", email="d@e.co"
        )
        self.region = Region.objects.create(code="77", name="Msk", distributor=self.distributor)

        client_user = User.objects.create_user(username="+79001110000", password="pw")
        self.client_profile = ClientProfile.objects.create(
            user=client_user, inn="5556667778", company_name="Автосервис",
            contact_name="Иван Иванов", phone="+79001110000",
            region=self.region, city="Москва", distributor=self.distributor,
        )
        self.store = Store.objects.create(
            client=self.client_profile, name="Точка 1", address="Москва, пр. Мира 22"
        )
        self.client_token = AuthToken.objects.create(key="client-token", user=client_user)
        self.distributor_token = AuthToken.objects.create(key="dist-token", user=self.distributor_user)

        courier_user = User.objects.create_user(username="+79002223344", password="pw")
        courier_user.first_name = "Пётр"
        courier_user.last_name = "Курьеров"
        courier_user.save()
        courier_user.profile.role = "courier"
        courier_user.profile.save()
        self.courier = courier_user
        self.courier_token = AuthToken.objects.create(key="courier-token", user=courier_user)

    def _order(self, status="new", delivery_method="courier", with_store=True, courier=None):
        return Order.objects.create(
            client=self.client_profile,
            distributor=self.distributor,
            store=self.store if with_store else None,
            delivery_method=delivery_method,
            status=status,
            courier=courier,
        )

    def _task_for(self, order):
        return CourierTask.objects.filter(order=order).first()

    def test_paid_order_creates_delivery(self):
        order = self._order()
        self.assertIsNone(self._task_for(order))  # новый заказ — доставки ещё нет

        order.status = "paid"
        order.save()

        task = self._task_for(order)
        self.assertIsNotNone(task, "оплаченный заказ должен создавать доставку")
        self.assertEqual(task.task_type, "delivery")
        self.assertEqual(task.status, "created")
        # Адрес — точка, выбранная в заказе, а не просто город.
        self.assertEqual(task.address, "Москва, пр. Мира 22")
        self.assertEqual(task.contact_name, "Иван Иванов")
        self.assertEqual([e["status"] for e in task.status_history], ["created"])

    def test_confirmed_order_has_no_delivery_yet(self):
        order = self._order(status="confirmed")
        self.assertIsNone(self._task_for(order))

    def test_self_pickup_never_creates_delivery(self):
        order = self._order(status="paid", delivery_method="self_pickup")
        self.assertIsNone(self._task_for(order))

    def test_address_falls_back_to_city(self):
        order = self._order(status="paid", with_store=False)
        self.assertEqual(self._task_for(order).address, "Москва")

    def test_delivery_created_once(self):
        order = self._order(status="paid")
        order.status = "shipped"
        order.save()
        self.assertEqual(CourierTask.objects.filter(order=order).count(), 1)

    def test_courier_assigned_on_order_syncs_to_delivery(self):
        order = self._order(status="paid")
        order.courier = self.courier
        order.save()

        task = self._task_for(order)
        self.assertEqual(task.courier, self.courier)
        self.assertEqual(task.status, "assigned")
        self.assertEqual([e["status"] for e in task.status_history], ["created", "assigned"])

    def test_distributor_assigns_courier(self):
        order = self._order(status="paid")
        task = self._task_for(order)

        response = self.http.post(
            f"/api/distributor/delivery-tasks/{task.id}/status/",
            data={"status": "assigned", "courierId": str(self.courier.id)},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.distributor_token.key}",
        )
        self.assertEqual(response.status_code, 200, response.content)

        task.refresh_from_db()
        self.assertEqual(task.status, "assigned")
        self.assertEqual(task.courier, self.courier)

    def test_courier_takes_job_then_finishes(self):
        order = self._order(status="paid", courier=self.courier)
        task = self._task_for(order)
        self.assertEqual(task.status, "assigned")

        # «Взяться за работу»
        response = self.http.post(
            f"/api/courier/tasks/{task.id}/status/",
            data={"status": "in_progress"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.courier_token.key}",
        )
        self.assertEqual(response.status_code, 200, response.content)
        task.refresh_from_db()
        self.assertEqual(task.status, "in_progress")

        # «Завершить доставку»
        response = self.http.post(
            f"/api/courier/tasks/{task.id}/status/",
            data={"status": "delivered"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.courier_token.key}",
        )
        self.assertEqual(response.status_code, 200, response.content)
        task.refresh_from_db()
        self.assertEqual(task.status, "delivered")

        # У клиента в истории — все четыре этапа с датами.
        self.assertEqual(
            [e["status"] for e in task.status_history],
            ["created", "assigned", "in_progress", "delivered"],
        )

    def test_client_sees_delivery_of_paid_order(self):
        order = self._order(status="paid", courier=self.courier)
        response = self.http.get(
            "/api/courier-tasks/?active=true",
            HTTP_AUTHORIZATION=f"Bearer {self.client_token.key}",
        )
        self.assertEqual(response.status_code, 200, response.content)
        items = response.json()["results"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["orderId"], str(order.id))
        self.assertEqual(items[0]["courierName"], "Пётр Курьеров")
        self.assertEqual(items[0]["courierPhone"], "+79002223344")
