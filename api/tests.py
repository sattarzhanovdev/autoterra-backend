import json

from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from .models import (
    Attachment,
    AuthToken,
    ClientProfile,
    ColorRequest,
    CourierTask,
    Distributor,
    ExpertTicket,
    Order,
    Product,
    Purchase,
    Region,
    Store,
)


class ClientRegistrationTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(username="+79990000001", password="managerpass")
        self.north_distributor = Distributor.objects.create(
            name="AutoTerra Север",
            inn="7701000001",
            regions=["Москва"],
            phone="+79990000002",
            email="north@example.com",
        )
        self.south_distributor = Distributor.objects.create(
            name="AutoTerra Юг",
            inn="2301000001",
            regions=["Краснодарский край"],
            phone="+79990000003",
            email="south@example.com",
        )
        self.moscow = Region.objects.create(
            code="msk",
            name="Москва",
            distributor=self.north_distributor,
            manager=self.manager,
        )
        self.krasnodar = Region.objects.create(
            code="krasnodar",
            name="Краснодарский край",
            distributor=self.south_distributor,
        )

    def _payload(self, **overrides):
        payload = {
            "inn": "7701234567",
            "companyName": "СТО Тест",
            "region": self.moscow.name,
            "category": "B",
            "contactName": "Иван Петров",
            "phone": "+79991112233",
            "email": "client@example.com",
            "password": "clientpass",
            "registrationSource": "client",
        }
        payload.update(overrides)
        return payload

    def _post_register(self, payload):
        return self.client.post(
            "/api/register/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def _existing_client(self, inn="7701234567", region="Москва"):
        user = User.objects.create_user(username=f"+7{ClientProfile.objects.count() + 1:010d}", password="pass123")
        return ClientProfile.objects.create(
            user=user,
            inn=inn,
            company_name="СТО Уже Есть",
            category="b",
            region=region,
            city=region,
            contact_name="Пётр Иванов",
            phone=user.username,
            distributor=self.north_distributor if region == self.moscow.name else self.south_distributor,
            status="approved",
        )

    def test_successful_registration_creates_under_review_client(self):
        response = self._post_register(self._payload())

        self.assertEqual(response.status_code, 201)
        client = ClientProfile.objects.get(inn="7701234567", region=self.moscow.name)
        self.assertEqual(client.company_name, "СТО Тест")
        self.assertEqual(client.status, "under_review")
        self.assertEqual(client.registration_source, "client")
        self.assertEqual(client.manager, self.manager)

    def test_duplicate_inn_in_same_region_is_rejected(self):
        self._existing_client()

        response = self._post_register(self._payload(phone="+79991112234"))
        body = response.json()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(body["code"], "inn_duplicate")
        self.assertEqual(ClientProfile.objects.filter(inn="7701234567", region=self.moscow.name).count(), 1)

    def test_registration_routes_client_by_region_distributor(self):
        response = self._post_register(
            self._payload(
                region=self.krasnodar.name,
                phone="+79991112235",
            )
        )

        self.assertEqual(response.status_code, 201)
        client = ClientProfile.objects.get(inn="7701234567", region=self.krasnodar.name)
        self.assertEqual(client.distributor, self.south_distributor)
        self.assertEqual(response.json()["distributor"]["id"], str(self.south_distributor.id))

    def test_same_inn_in_another_region_creates_branch_under_review(self):
        self._existing_client()

        response = self._post_register(
            self._payload(
                region=self.krasnodar.name,
                phone="+79991112236",
            )
        )
        body = response.json()

        self.assertEqual(response.status_code, 201)
        self.assertTrue(body["isBranch"])
        self.assertTrue(body["requiresAdminApproval"])
        branch = ClientProfile.objects.get(inn="7701234567", region=self.krasnodar.name)
        self.assertEqual(branch.status, "under_review")
        self.assertEqual(ClientProfile.objects.filter(inn="7701234567").count(), 2)


class PurchaseCreateTests(TestCase):
    def setUp(self):
        self.client_user = User.objects.create_user(username="+79990001000", password="clientpass")
        self.distributor_user = User.objects.create_user(username="+79990002000", password="distpass")
        self.distributor = Distributor.objects.create(
            user=self.distributor_user,
            name="AutoTerra Север",
            inn="7701000001",
            regions=["Москва"],
            phone="+79990000002",
            email="north@example.com",
        )
        self.profile = ClientProfile.objects.create(
            user=self.client_user,
            inn="7701234567",
            company_name="СТО Тест",
            category="b",
            region="Москва",
            city="Москва",
            contact_name="Иван Петров",
            phone="+79990001000",
            distributor=self.distributor,
            status="approved",
        )
        self.client_token = AuthToken.objects.create(key="client-token", user=self.client_user)
        self.distributor_token = AuthToken.objects.create(key="dist-token", user=self.distributor_user)

    def _headers(self, token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token.key}"}

    def _items(self):
        return json.dumps(
            [
                {
                    "sku": "LAK-015",
                    "name": "Лак HS 2+1",
                    "category": "Лаки",
                    "quantity": 2,
                    "price": 2800,
                }
            ]
        )

    def _file(self, content=b"document-bytes"):
        return SimpleUploadedFile("upd.jpg", content, content_type="image/jpeg")

    def _post_purchase(self, **overrides):
        data = {
            "inn": self.profile.inn,
            "documentNumber": "UPD-001",
            "date": "2026-06-01",
            "totalAmount": "5600",
            "items": self._items(),
            "document": self._file(),
        }
        data.update(overrides)
        return self.client.post(
            "/api/purchases/create/",
            data=data,
            **self._headers(self.client_token),
        )

    def test_create_purchase_with_attachment_and_items(self):
        response = self._post_purchase()

        self.assertEqual(response.status_code, 201)
        purchase = Purchase.objects.get(document_number="UPD-001")
        self.assertEqual(purchase.status, "pending_verification")
        self.assertEqual(purchase.client, self.profile)
        self.assertEqual(purchase.distributor, self.distributor)
        self.assertTrue(purchase.document_file.name)
        self.assertTrue(purchase.document_hash)
        self.assertEqual(purchase.items.count(), 1)
        self.assertEqual(Attachment.objects.count(), 1)

    def test_exact_duplicate_purchase_is_rejected(self):
        self._post_purchase()

        response = self._post_purchase(document=self._file())

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "purchase_duplicate")
        self.assertEqual(Purchase.objects.count(), 1)

    def test_similar_duplicate_goes_to_duplicate_review(self):
        self._post_purchase()

        response = self._post_purchase(documentNumber="UPD-001-COPY", document=self._file(b"another-file"))

        self.assertEqual(response.status_code, 201)
        purchase = Purchase.objects.get(document_number="UPD-001-COPY")
        self.assertEqual(purchase.status, "duplicate_review")

    def test_distributor_can_confirm_and_reject_purchases(self):
        self._post_purchase()
        purchase = Purchase.objects.get(document_number="UPD-001")

        confirm_response = self.client.post(
            f"/api/distributor/purchases/{purchase.id}/confirm/",
            **self._headers(self.distributor_token),
        )
        self.assertEqual(confirm_response.status_code, 200)
        purchase.refresh_from_db()
        self.assertEqual(purchase.status, "verified")

        reject_response = self.client.post(
            f"/api/distributor/purchases/{purchase.id}/reject/",
            data=json.dumps({"reason": "Документ не читается"}),
            content_type="application/json",
            **self._headers(self.distributor_token),
        )
        self.assertEqual(reject_response.status_code, 200)
        purchase.refresh_from_db()
        self.assertEqual(purchase.status, "rejected")
        self.assertEqual(purchase.rejection_reason, "Документ не читается")

    def test_color_request_accepts_attachment(self):
        response = self.client.post(
            "/api/color-requests/create/",
            data={
                "carBrand": "Toyota",
                "carModel": "Camry",
                "vin": "JTDBT923391234567",
                "colorCode": "1F7",
                "colorName": "Silver",
                "attachments": self._file(),
            },
            **self._headers(self.client_token),
        )

        self.assertEqual(response.status_code, 201)
        request = ColorRequest.objects.get()
        self.assertEqual(Attachment.objects.filter(object_id=request.id).count(), 1)
        self.assertEqual(response.json()["request"]["attachments"][0]["fileType"], "image")

    def test_ticket_accepts_attachment(self):
        response = self.client.post(
            "/api/tickets/create/",
            data={
                "question": "Почему подрывает лак?",
                "category": "Дефекты",
                "attachments": self._file(),
            },
            **self._headers(self.client_token),
        )

        self.assertEqual(response.status_code, 201)
        ticket = ExpertTicket.objects.get()
        self.assertEqual(Attachment.objects.filter(object_id=ticket.id).count(), 1)
        self.assertEqual(response.json()["ticket"]["attachments"][0]["name"].endswith(".jpg"), True)

    def test_courier_proof_accepts_attachment(self):
        task = CourierTask.objects.create(
            client=self.profile,
            type="delivery",
            address="Москва, ул. Тестовая, 1",
            scheduled_time=timezone.now(),
            contact_name="Иван Петров",
            contact_phone="+79990001000",
        )

        response = self.client.post(
            f"/api/courier-tasks/{task.id}/proof/",
            data={"proof": self._file()},
            **self._headers(self.client_token),
        )

        self.assertEqual(response.status_code, 201)
        task.refresh_from_db()
        self.assertTrue(task.photo_proof)
        self.assertEqual(response.json()["task"]["attachments"][0]["fileType"], "image")

    def test_invalid_attachment_type_is_rejected(self):
        response = self.client.post(
            "/api/tickets/create/",
            data={
                "question": "Проверка",
                "category": "Другое",
                "attachments": SimpleUploadedFile("bad.exe", b"bad", content_type="application/octet-stream"),
            },
            **self._headers(self.client_token),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_file_type")

    def test_distributor_sees_only_own_clients(self):
        other_user = User.objects.create_user(username="+79990003000", password="distpass")
        other_dist = Distributor.objects.create(
            user=other_user,
            name="AutoTerra Юг",
            inn="2301000001",
            regions=["Краснодар"],
            phone="+79990000003",
            email="south@example.com",
        )
        other_client_user = User.objects.create_user(username="+79990004000", password="pass123")
        ClientProfile.objects.create(
            user=other_client_user,
            inn="2301234567",
            company_name="Чужое СТО",
            category="b",
            region="Краснодар",
            city="Краснодар",
            contact_name="Пётр",
            phone="+79990004000",
            distributor=other_dist,
            status="approved",
        )

        response = self.client.get("/api/distributor/clients/", **self._headers(self.distributor_token))
        inns = [item["inn"] for item in response.json()["results"]]

        self.assertEqual(response.status_code, 200)
        self.assertIn(self.profile.inn, inns)
        self.assertNotIn("2301234567", inns)

    def test_distributor_order_workflow(self):
        store = Store.objects.create(client=self.profile, name="Основная точка", address="Москва")
        order = Order.objects.create(client=self.profile, store=store, distributor=self.distributor)

        response = self.client.post(
            f"/api/distributor/orders/{order.id}/accept/",
            **self._headers(self.distributor_token),
        )
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "accepted")

        response = self.client.post(
            f"/api/distributor/orders/{order.id}/reject/",
            data=json.dumps({"reason": "Нет товара на складе"}),
            content_type="application/json",
            **self._headers(self.distributor_token),
        )
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "rejected")
        self.assertEqual(order.rejection_reason, "Нет товара на складе")

    def test_distributor_can_upload_stock(self):
        response = self.client.post(
            "/api/distributor/stock/upload/",
            data=json.dumps(
                {
                    "items": [
                        {
                            "sku": "PRM-001",
                            "name": "Грунт 2K",
                            "category": "Грунтовки",
                            "quantity": 12,
                            "status": "inStock",
                        }
                    ]
                }
            ),
            content_type="application/json",
            **self._headers(self.distributor_token),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["created"], 1)
        product = Product.objects.get(sku="PRM-001")
        self.assertEqual(product.distributor, self.distributor)
        self.assertEqual(product.quantity, 12)

    def test_distributor_assigns_courier_and_courier_updates_status(self):
        courier_group = Group.objects.create(name="courier")
        courier = User.objects.create_user(username="+79990005000", password="courierpass")
        courier.groups.add(courier_group)
        courier_token = AuthToken.objects.create(key="courier-token", user=courier)
        task = CourierTask.objects.create(
            client=self.profile,
            type="delivery",
            address="Москва, ул. Тестовая, 1",
            scheduled_time=timezone.now(),
            contact_name="Иван Петров",
            contact_phone="+79990001000",
        )

        assign_response = self.client.post(
            f"/api/courier/tasks/{task.id}/assign/",
            data=json.dumps({"courierId": str(courier.id)}),
            content_type="application/json",
            **self._headers(self.distributor_token),
        )
        self.assertEqual(assign_response.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.assigned_courier, courier)
        self.assertEqual(task.status, "assigned")

        list_response = self.client.get("/api/courier/tasks/", **self._headers(courier_token))
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(len(list_response.json()["results"]), 1)

        status_response = self.client.post(
            f"/api/courier/tasks/{task.id}/status/",
            data=json.dumps({"status": "picked_up", "comment": "Лючок забран"}),
            content_type="application/json",
            **self._headers(courier_token),
        )
        self.assertEqual(status_response.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, "picked_up")
        self.assertEqual(task.status_history[-1]["comment"], "Лючок забран")

        comment_response = self.client.post(
            f"/api/courier/tasks/{task.id}/comment/",
            data=json.dumps({"comment": "Еду на станцию"}),
            content_type="application/json",
            **self._headers(courier_token),
        )
        self.assertEqual(comment_response.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.courier_comment, "Еду на станцию")

    def test_color_request_with_courier_pickup_creates_task(self):
        response = self.client.post(
            "/api/color-requests/create/",
            data={
                "carBrand": "Toyota",
                "carModel": "Camry",
                "vin": "JTDBT923391234567",
                "colorCode": "1F7",
                "courierPickup": "true",
                "pickupAddress": "Москва, Цветочная, 5",
            },
            **self._headers(self.client_token),
        )

        self.assertEqual(response.status_code, 201)
        task = CourierTask.objects.get(color_request__isnull=False)
        self.assertEqual(task.type, "pickup")
        self.assertEqual(task.address, "Москва, Цветочная, 5")
