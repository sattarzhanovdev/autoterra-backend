from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from .models import (
    Purchase, ClientProfile, Region, Distributor, AuthToken, Attachment,
)


class ReceiptUploadTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.dist_user = User.objects.create_user(username="+79001110001", password="pw")
        self.distributor = Distributor.objects.create(
            user=self.dist_user, name="Main Dist", inn="123", phone="1", email="d@e.co"
        )
        self.region = Region.objects.create(code="77", name="Москва", distributor=self.distributor)
        self.client_user = User.objects.create_user(username="client_user")
        self.client_profile = ClientProfile.objects.create(
            user=self.client_user, inn="1234567890", company_name="Test Client",
            region=self.region, distributor=self.distributor, phone="111",
            city="Москва", contact_name="Contact",
        )
        self.token = AuthToken.objects.create(key="client-token", user=self.client_user)
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.token.key}"}

    def _post_with_file(self, content_type):
        # Mobile clients send octet-stream by default; a fixed client sends image/jpeg.
        receipt = SimpleUploadedFile("receipt.jpg", b"\xff\xd8\xff\xe0fakejpeg", content_type=content_type)
        return self.client.post(
            "/api/purchases/create/",
            data={
                "document_number": "INV-RCPT-1",
                "date": "2026-06-06",
                "amount": "1500",
                "document": receipt,
            },
            **self.auth,
        )

    def test_octet_stream_receipt_is_saved(self):
        resp = self._post_with_file("application/octet-stream")
        self.assertEqual(resp.status_code, 201, resp.content)
        purchase = Purchase.objects.get(document_number="INV-RCPT-1")
        atts = Attachment.objects.filter(object_id=purchase.pk)
        self.assertEqual(atts.count(), 1, "receipt attachment should be persisted")

    def test_image_jpeg_receipt_is_saved(self):
        resp = self._post_with_file("image/jpeg")
        self.assertEqual(resp.status_code, 201, resp.content)
        purchase = Purchase.objects.get(document_number="INV-RCPT-1")
        self.assertEqual(Attachment.objects.filter(object_id=purchase.pk).count(), 1)

    def test_disallowed_content_type_rejects_purchase(self):
        bad = SimpleUploadedFile("receipt.jpg", b"x", content_type="text/html")
        resp = self.client.post(
            "/api/purchases/create/",
            data={"document_number": "INV-BAD", "date": "2026-06-06", "amount": "10", "document": bad},
            **self.auth,
        )
        self.assertEqual(resp.status_code, 400)
        # Purchase must be rolled back, not left without its proof.
        self.assertFalse(Purchase.objects.filter(document_number="INV-BAD").exists())
