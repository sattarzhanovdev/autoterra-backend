import json
from django.test import TestCase, Client
from django.contrib.auth.models import User
from .models import Distributor, AuthToken, Profile

class MeEndpointTests(TestCase):
    def setUp(self):
        self.client = Client()
        
        # 1. Distributor
        self.dist_user = User.objects.create_user(username="dist_user", password="password")
        self.dist_user.profile.role = "distributor"
        self.dist_user.profile.save()
        self.distributor = Distributor.objects.create(
            user=self.dist_user, name="Main Dist", inn="12345"
        )
        self.dist_token = AuthToken.objects.create(key="dist-tok", user=self.dist_user)
        self.dist_headers = {"HTTP_AUTHORIZATION": f"Bearer {self.dist_token.key}"}

        # 2. Client
        self.client_user = User.objects.create_user(username="client_user", password="password")
        self.client_user.profile.role = "client"
        self.client_user.profile.save()
        self.client_token = AuthToken.objects.create(key="client-tok", user=self.client_user)
        self.client_headers = {"HTTP_AUTHORIZATION": f"Bearer {self.client_token.key}"}

    def test_me_distributor(self):
        resp = self.client.get("/api/auth/me/", **self.dist_headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["role"], "distributor")
        self.assertIn("distributor", data)

    def test_me_unauthorized(self):
        resp = self.client.get("/api/auth/me/")
        self.assertEqual(resp.status_code, 401)
