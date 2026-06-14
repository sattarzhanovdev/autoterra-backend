import json
from django.test import TestCase, Client
from django.contrib.auth.models import User
from .models import Distributor, Product, AuthToken, Profile

class StockUploadTests(TestCase):
    def setUp(self):
        self.client = Client()
        
        # Create distributor user
        self.dist_user = User.objects.create_user(username="dist_user", password="password")
        self.dist_user.profile.role = "distributor"
        self.dist_user.profile.save()
        
        self.distributor = Distributor.objects.create(
            user=self.dist_user,
            name="Main Distributor",
            inn="7701000001"
        )
        self.token = AuthToken.objects.create(key="dist-token", user=self.dist_user)
        self.auth_headers = {"HTTP_AUTHORIZATION": f"Bearer {self.token.key}"}

        # Create a regular client user (should not have access)
        self.client_user = User.objects.create_user(username="client_user", password="password")
        self.client_user.profile.role = "client"
        self.client_user.profile.save()
        self.client_token = AuthToken.objects.create(key="client-token", user=self.client_user)
        self.client_headers = {"HTTP_AUTHORIZATION": f"Bearer {self.client_token.key}"}

    def test_stock_upload_creates_new_products(self):
        """Test that uploading new items creates Product records."""
        payload = {
            "items": [
                {
                    "sku": "NEW-SKU-01",
                    "name": "New Product 1",
                    "category": "Paints",
                    "brand": "Brand A",
                    "price": "1500.50",
                    "quantity": 10
                },
                {
                    "sku": "NEW-SKU-02",
                    "name": "New Product 2",
                    "category": "Primers",
                    "brand": "Brand B",
                    "price": 800,
                    "quantity": 5
                }
            ]
        }
        
        response = self.client.post(
            "/api/distributor/stock/upload/",
            data=json.dumps(payload),
            content_type="application/json",
            **self.auth_headers
        )
        
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["processed"], 2)
        
        # Verify DB
        p1 = Product.objects.get(sku="NEW-SKU-01", distributor=self.distributor)
        self.assertEqual(p1.name, "New Product 1")
        self.assertEqual(float(p1.price), 1500.50)
        self.assertEqual(p1.quantity, 10)
        self.assertEqual(p1.status, "inStock")

    def test_stock_upload_updates_existing_products(self):
        """Test that uploading items with existing SKU updates them instead of duplicating."""
        # 1. Initial product
        Product.objects.create(
            distributor=self.distributor,
            sku="EXISTING-01",
            name="Old Name",
            price=100.0,
            quantity=1
        )
        
        # 2. Upload same SKU
        payload = {
            "items": [{
                "sku": "EXISTING-01",
                "name": "Updated Name",
                "category": "Updated Category",
                "price": 250.0,
                "quantity": 50,
                "status": "inStock"
            }]
        }
        
        response = self.client.post(
            "/api/distributor/stock/upload/",
            data=json.dumps(payload),
            content_type="application/json",
            **self.auth_headers
        )
        
        self.assertEqual(response.status_code, 200)
        
        # 3. Verify update
        prod = Product.objects.get(sku="EXISTING-01", distributor=self.distributor)
        self.assertEqual(Product.objects.filter(sku="EXISTING-01").count(), 1)
        self.assertEqual(prod.name, "Updated Name")
        self.assertEqual(prod.quantity, 50)
        self.assertEqual(float(prod.price), 250.0)

    def test_access_denied_for_clients(self):
        """Test that clients cannot use the stock upload endpoint."""
        response = self.client.post(
            "/api/distributor/stock/upload/",
            data=json.dumps({"items": []}),
            content_type="application/json",
            **self.client_headers
        )
        self.assertEqual(response.status_code, 403)

    def test_manual_add_product_success(self):
        """Test adding a single product manually via the new endpoint."""
        payload = {
            "sku": "MANUAL-001",
            "name": "Handmade Paint",
            "category": "Special",
            "price": 2000,
            "quantity": 3
        }
        response = self.client.post(
            "/api/distributor/stock/add/",
            data=json.dumps(payload),
            content_type="application/json",
            **self.auth_headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["product"]["sku"], "MANUAL-001")
        
        # Verify in DB
        self.assertTrue(Product.objects.filter(sku="MANUAL-001", distributor=self.distributor).exists())

    def test_manual_add_duplicate_sku_fails(self):
        """Test that adding a product with an existing SKU fails."""
        Product.objects.create(
            distributor=self.distributor,
            sku="DUP-123",
            name="Original"
        )
        payload = {
            "sku": "DUP-123",
            "name": "Duplicate"
        }
        response = self.client.post(
            "/api/distributor/stock/add/",
            data=json.dumps(payload),
            content_type="application/json",
            **self.auth_headers
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("уже существует", response.json()["detail"])

