"""Malformed input must fail without creating records or changing balances."""
import tempfile
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings

from .models import Attachment, AuthToken, CourierTask, Order
from .serializers import RegistrationSerializer, PurchaseSerializer
from .test_bonus_account import _Base
from .views import _delivery_status_error, _limit
from django.test import RequestFactory


class JsonValidationTests(SimpleTestCase):
    def test_invalid_json_and_non_objects_return_json_400(self):
        for body in ('[1]', 'null', 'true', '42', '"text"', '{', b'\xff'):
            with self.subTest(body=body):
                response = self.client.post('/api/login/', body, content_type='application/json')
                self.assertEqual(response.status_code, 400)
                self.assertIn('detail', response.json())

    def test_auth_rejects_wrong_field_types(self):
        for path, payload in [('/api/login/', {'phone': 123}),
                              ('/api/login/', {'password': []}),
                              ('/api/auth/password-reset/', {'inn': {}})]:
            self.assertEqual(self.client.post(path, payload, content_type='application/json').status_code, 400)

    def test_erp_arrays_remain_supported(self):
        for endpoint in ('stock-update', 'catalog-sync'):
            path = f'/api/integration/erp/{endpoint}/'
            self.assertEqual(self.client.post(path, [{'sku': 'a'}], content_type='application/json').status_code, 401)
            self.assertEqual(self.client.post(path, [None], content_type='application/json').status_code, 400)

    def test_serializers_reject_invalid_types(self):
        for field in ('password', 'company_name', 'contact_name', 'store_address', 'region_id'):
            self.assertFalse(RegistrationSerializer({field: []}).is_valid())
        for field in ('document_number', 'date'):
            self.assertFalse(PurchaseSerializer({field: 123}).is_valid())

    def test_negative_list_offsets_are_safe(self):
        request = RequestFactory().get('/', {'limit': -1, 'offset': -2})
        self.assertEqual(_limit(request, [1, 2, 3]), [])


class AuditRegressionTests(_Base):
    def setUp(self):
        super().setUp()
        self.auth = {'HTTP_AUTHORIZATION': f'Bearer {self.token}'}
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        override = override_settings(MEDIA_ROOT=media.name)
        override.enable()
        self.addCleanup(override.disable)
        self.task = CourierTask.objects.create(client=self.client_profile, address='Address', time_slot='10:00')

    def test_cancel_history_is_persisted(self):
        response = self.http.post(f'/api/courier-tasks/{self.task.pk}/cancel/', **self.auth)
        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status_history[-1]['status'], 'cancelled')

    def test_proof_of_missing_or_foreign_task_is_not_successful(self):
        self.assertEqual(self.http.post('/api/courier-tasks/999999/proof/', **self.auth).status_code, 404)
        self.task.client = self._client('other', '2223334445')
        self.task.save()
        self.assertEqual(self.http.post(f'/api/courier-tasks/{self.task.pk}/proof/', **self.auth).status_code, 404)

    def test_invalid_file_batch_creates_no_partial_attachments(self):
        response = self.http.post(f'/api/courier-tasks/{self.task.pk}/proof/', {
            'files': [SimpleUploadedFile('proof.jpg', b'photo', content_type='image/jpeg'),
                      SimpleUploadedFile('bad.html', b'<script/>', content_type='text/html')],
        }, **self.auth)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Attachment.objects.exists())

    def test_empty_proof_is_rejected(self):
        self.assertEqual(self.http.post(f'/api/courier-tasks/{self.task.pk}/proof/', **self.auth).status_code, 400)

    def test_courier_invalid_proof_does_not_change_status(self):
        courier = User.objects.create_user(username='courier')
        courier.profile.role = 'courier'
        courier.profile.save()
        AuthToken.objects.create(user=courier, key='courier-audit')
        self.task.courier = courier
        self.task.save()
        response = self.http.post(f'/api/courier/tasks/{self.task.pk}/status/', {
            'status': 'in_progress',
            'proof_photo': SimpleUploadedFile('bad.html', b'bad', content_type='text/html'),
        }, HTTP_AUTHORIZATION='Bearer courier-audit')
        self.assertEqual(response.status_code, 400)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'created')

    def test_admin_cannot_assign_client_as_courier(self):
        admin = User.objects.create_superuser(username='audit-admin', password='password')
        AuthToken.objects.create(user=admin, key='admin-audit')
        path = f'/api/courier/tasks/{self.task.pk}/assign/'
        for value in (self.client_profile.user_id, 'bad', {}, 999999):
            response = self.http.post(path, {'courierId': value}, content_type='application/json',
                                      HTTP_AUTHORIZATION='Bearer admin-audit')
            self.assertEqual(response.status_code, 400)
        self.task.refresh_from_db()
        self.assertIsNone(self.task.courier)

    def test_oversized_order_is_rejected_before_stock_reservation(self):
        self.product.price = Decimal('9999999999.99')
        self.product.save()
        response = self.http.post('/api/orders/create/', {'items': [
            {'productId': self.product.pk, 'quantity': 2}]}, content_type='application/json', **self.auth)
        self.assertEqual(response.status_code, 400)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, 1000)
        self.assertFalse(Order.objects.exists())

    def test_invalid_order_fields_return_400(self):
        for extra in ({'storeId': 'bad'}, {'deliveryMethod': {}}, {'comment': []}):
            body = {'items': [{'productId': self.product.pk, 'quantity': 1}], **extra}
            self.assertEqual(self.http.post('/api/orders/create/', body,
                                            content_type='application/json', **self.auth).status_code, 400)

    def test_completed_delivery_cannot_be_reopened(self):
        for status in ('delivered', 'returned', 'cancelled'):
            self.task.status = status
            self.assertEqual(_delivery_status_error(self.task, 'assigned').status_code, 400)
            self.assertIsNone(_delivery_status_error(self.task, status))

    def test_repeated_delivery_confirmation_is_idempotent(self):
        order = self._order(100, status='fulfilled')
        self.task.order = order
        self.task.status = 'delivered'
        self.assertIsNone(_delivery_status_error(self.task, 'delivered'))
        self.task.status = 'in_progress'
        self.assertEqual(_delivery_status_error(self.task, 'delivered').status_code, 400)

    def test_oversized_adjustment_rolls_back_items_and_stock(self):
        admin = User.objects.create_superuser(username='adjust-admin', password='password')
        AuthToken.objects.create(user=admin, key='adjust-audit')
        order = self._order(Decimal('9999999999.99'), status='new')
        item = order.items.get()
        response = self.http.post(f'/api/orders/{order.pk}/adjust/', {
            'items': [{'itemId': item.pk, 'quantity': 2}],
        }, content_type='application/json', HTTP_AUTHORIZATION='Bearer adjust-audit')
        self.assertEqual(response.status_code, 400)
        order.refresh_from_db()
        item.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(order.status, 'new')
        self.assertEqual(item.quantity, 1)
        self.assertEqual(self.product.quantity, 1000)
