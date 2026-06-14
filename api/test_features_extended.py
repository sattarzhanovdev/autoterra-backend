import json
from django.test import TestCase, Client
from django.contrib.auth.models import User
from .models import (
    Distributor, Product, AuthToken, Profile, 
    ColorRequest, CourierTask, ClientProfile, Region
)

class ExtendedFeaturesTests(TestCase):
    def setUp(self):
        self.client = Client()
        
        # 1. Setup Distributor
        self.dist_user = User.objects.create_user(username="dist_user", password="password")
        self.dist_user.profile.role = "distributor"
        self.dist_user.profile.save()
        self.distributor = Distributor.objects.create(
            user=self.dist_user, name="Region Dist", inn="7700000001"
        )
        self.dist_token = AuthToken.objects.create(key="dist-tok", user=self.dist_user)
        self.dist_headers = {"HTTP_AUTHORIZATION": f"Bearer {self.dist_token.key}"}

        # 2. Setup Courier
        self.courier_user = User.objects.create_user(username="courier_bob", password="password")
        self.courier_user.profile.role = "courier"
        self.courier_user.profile.save()

        # 3. Setup Region & Client
        self.region = Region.objects.create(code="77", name="Moscow", distributor=self.distributor)
        self.client_user = User.objects.create_user(username="+79001112233", password="password")
        self.client_user.profile.role = "client"
        self.client_user.profile.save()
        self.client_profile = ClientProfile.objects.create(
            user=self.client_user,
            inn="1234567890",
            company_name="Auto Shop",
            region=self.region,
            distributor=self.distributor,
            phone="+79001112233",
            city="Moscow",
            contact_name="Owner"
        )
        self.client_token = AuthToken.objects.create(key="client-tok", user=self.client_user)
        self.client_headers = {"HTTP_AUTHORIZATION": f"Bearer {self.client_token.key}"}

    def test_color_request_edit_and_cancel(self):
        """Test that a client can edit and then cancel their color request."""
        # Create initial request
        req = ColorRequest.objects.create(
            client=self.client_profile,
            car_brand="BMW",
            car_model="X5",
            vin="VIN123",
            status="created"
        )

        # 1. Update request
        update_payload = {
            "carBrand": "Audi",
            "carModel": "Q7",
            "comment": "New instructions"
        }
        resp = self.client.post(
            f"/api/color-requests/{req.id}/update/",
            data=json.dumps(update_payload),
            content_type="application/json",
            **self.client_headers
        )
        self.assertEqual(resp.status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.car_brand, "Audi")
        self.assertEqual(req.comment, "New instructions")

        # 2. Cancel request
        resp = self.client.post(
            f"/api/color-requests/{req.id}/cancel/",
            **self.client_headers
        )
        self.assertEqual(resp.status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.status, "cancelled")

        # 3. Try to update cancelled request (should fail or we can decide policy, usually restricted)
        # In our current view, it only checks if status is 'created'
        resp = self.client.post(
            f"/api/color-requests/{req.id}/update/",
            data=json.dumps({"carBrand": "Tesla"}),
            content_type="application/json",
            **self.client_headers
        )
        self.assertEqual(resp.status_code, 400) # Should fail as status is 'cancelled' not 'created'

    def test_distributor_delivery_management(self):
        """Test distributor viewing regional tasks and assigning couriers."""
        # 1. Create a task from client
        task = CourierTask.objects.create(
            client=self.client_profile,
            task_type="pickup",
            address="Client St 1",
            status="created"
        )

        # 2. Distributor views tasks
        resp = self.client.get("/api/distributor/delivery-tasks/", **self.dist_headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["results"]), 1)
        self.assertEqual(resp.json()["results"][0]["address"], "Client St 1")

        # 3. Distributor assigns courier
        assign_payload = {
            "courierId": self.courier_user.id,
            "status": "assigned"
        }
        resp = self.client.post(
            f"/api/distributor/delivery-tasks/{task.id}/status/",
            data=json.dumps(assign_payload),
            content_type="application/json",
            **self.dist_headers
        )
        self.assertEqual(resp.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, "assigned")
        self.assertEqual(task.courier, self.courier_user)

    def test_cross_distributor_access_denied(self):
        """Ensure distributor cannot see or manage tasks from another distributor's region."""
        # Create another distributor
        other_dist_user = User.objects.create_user(username="other_dist", password="password")
        other_dist_user.profile.role = "distributor"
        other_dist_user.profile.save()
        other_dist = Distributor.objects.create(user=other_dist_user, name="Other Dist", inn="7700000002")
        other_token = AuthToken.objects.create(key="other-tok", user=other_dist_user)
        other_headers = {"HTTP_AUTHORIZATION": f"Bearer {other_token.key}"}

        # Task belongs to self.distributor (via self.client_profile)
        task = CourierTask.objects.create(
            client=self.client_profile,
            task_type="delivery",
            status="created"
        )

        # Other distributor tries to see tasks
        resp = self.client.get("/api/distributor/delivery-tasks/", **other_headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["results"]), 0) # Should be empty for them

        # Other distributor tries to update task status
        resp = self.client.post(
            f"/api/distributor/delivery-tasks/{task.id}/status/",
            data=json.dumps({"status": "cancelled"}),
            content_type="application/json",
            **other_headers
        )
        self.assertEqual(resp.status_code, 404) # Task not in their scope
