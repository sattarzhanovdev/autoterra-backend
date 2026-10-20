from django.contrib.auth.models import User
from django.test import override_settings

from .models import AuditLog
from .test_client_discount import _ClientDiscountBase


@override_settings(SECURE_SSL_REDIRECT=False, EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class RegistrationApprovalTests(_ClientDiscountBase):
    def setUp(self):
        super().setUp()
        self.path = f'/api/manager/clients/{self.client_profile.pk}/approve/'
        self.client_profile.status = 'new'
        self.client_profile.save(update_fields=['status'])

    def test_manager_approval_opens_catalog_and_orders_on_existing_session(self):
        self.assertEqual(self.http.get('/api/products/', **self.auth(self.token)).status_code, 403)
        data = self.http.get(f'/api/clients/{self.client_profile.pk}/settings/', **self.auth('manager')).json()
        self.assertTrue(data['canApprove'])
        self.assertFalse(data['canEdit'])
        response = self.http.post(self.path, {}, content_type='application/json', **self.auth('manager'))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['client']['status'], 'active')
        self.assertEqual(self.http.get('/api/auth/me/', **self.auth(self.token)).json()['status'], 'active')
        self.assertEqual(self.http.get('/api/products/', **self.auth(self.token)).status_code, 200)
        self.assertEqual(self.create_order().status_code, 201)
        self.assertEqual(self.http.post(self.path, {}, content_type='application/json', **self.auth('manager')).status_code, 200)
        self.assertEqual(AuditLog.objects.filter(action='Client status changed').count(), 1)
        self.assertEqual(self.set_discount('20', 'manager').status_code, 403)

    def test_foreign_region_and_unrelated_roles_cannot_approve(self):
        for key in ('courier', 'ai_expert', self.token):
            self.assertEqual(self.http.post(self.path, {}, content_type='application/json', **self.auth(key)).status_code, 403)
        self.region.manager = None
        self.region.save(update_fields=['manager'])
        self.assertEqual(self.http.post(self.path, {}, content_type='application/json', **self.auth('manager')).status_code, 404)
        self.assertEqual(self.fresh().status, 'new')

    def test_owner_and_admin_can_approve_but_not_restore_blocked_accounts(self):
        for role in ('distributor', 'admin'):
            self.client_profile.status = 'under_review'
            self.client_profile.save(update_fields=['status'])
            self.assertEqual(self.http.post(self.path, {}, content_type='application/json', **self.auth(role)).status_code, 200)
        for status in ('blocked', 'archived'):
            self.client_profile.status = status
            self.client_profile.save(update_fields=['status'])
            self.assertEqual(self.http.post(self.path, {}, content_type='application/json', **self.auth('manager')).status_code, 409)
            self.assertEqual(self.fresh().status, status)

    def test_approval_requires_active_distributor_and_can_repair_missing_assignment(self):
        self.distributor.is_active = False
        self.distributor.save(update_fields=['is_active'])
        self.assertEqual(self.http.post(self.path, {}, content_type='application/json', **self.auth('manager')).status_code, 409)
        self.assertEqual(self.fresh().status, 'new')
        self.distributor.is_active = True
        self.distributor.save(update_fields=['is_active'])
        self.client_profile.distributor = None
        self.client_profile.save(update_fields=['distributor'])
        self.assertEqual(self.http.post(self.path, {}, content_type='application/json', **self.auth('manager')).status_code, 200)
        self.assertEqual(self.fresh().distributor, self.distributor)

    def test_model_activation_synchronizes_account_access(self):
        User.objects.filter(pk=self.client_profile.user_id).update(is_active=False)
        self.client_profile.status = 'active'
        self.client_profile.save(update_fields=['status'])
        self.client_profile.user.refresh_from_db()
        self.assertTrue(self.client_profile.user.is_active)
