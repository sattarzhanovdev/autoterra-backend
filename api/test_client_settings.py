from . import test_client_discount as discounts
from .models import AuditLog, ClientProfile, Distributor, Region


class ClientSettingsTests(discounts._ClientDiscountBase):
    def setUp(self):
        super().setUp()
        self.settings_url = f'/api/clients/{self.client_profile.pk}/settings/'

    def patch_settings(self, data, key='distributor', url=None):
        return self.http.patch(url or self.settings_url, data, content_type='application/json', **self.auth(key))

    def test_choices_and_shop_category_apply_without_login(self):
        result = self.http.get(self.settings_url, **self.auth('distributor')).json()
        self.assertEqual(result['statuses'], [{'value': v, 'label': l} for v, l in ClientProfile.STATUS_CHOICES])
        self.assertEqual(result['categories'], [{'value': v, 'label': l} for v, l in ClientProfile.CATEGORY_CHOICES])
        self.assertEqual(self.http.get('/api/me/markup/', **self.auth(self.token)).status_code, 403)
        result = self.patch_settings({'category': 's'})
        self.assertEqual(result.status_code, 200, result.content)
        response = self.http.patch('/api/me/markup/', {'markupPercent': '15'}, content_type='application/json', **self.auth(self.token))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.http.get('/api/products/', **self.auth(self.token)).json()['results'][0]['markupPercent'], '15.00')
        self.assertEqual(self.patch_settings({'category': 'b'}).status_code, 200)
        self.assertEqual(self.http.get('/api/me/markup/', **self.auth(self.token)).status_code, 403)
        self.assertNotIn('markupPercent', self.http.get('/api/products/', **self.auth(self.token)).json()['results'][0])

    def test_status_changes_existing_session_and_archive_revokes_tokens(self):
        for status in ('blocked', 'under_review', 'new'):
            self.assertEqual(self.patch_settings({'status': status}).status_code, 200)
            result = self.http.get('/api/products/', **self.auth(self.token))
            self.assertEqual(result.status_code, 403)
            self.assertEqual(result.json()['status'], status)
        self.assertEqual(self.patch_settings({'status': 'active'}).status_code, 200)
        self.assertEqual(self.http.get('/api/products/', **self.auth(self.token)).status_code, 200)
        self.assertEqual(self.patch_settings({'status': 'archived'}).status_code, 200)
        self.client_profile.user.refresh_from_db()
        self.assertFalse(self.client_profile.user.is_active)
        self.assertFalse(self.client_profile.user.auth_tokens.exists())
        self.assertEqual(self.patch_settings({'status': 'active'}).status_code, 200)
        self.client_profile.user.refresh_from_db()
        self.assertTrue(self.client_profile.user.is_active)

    def test_permissions_on_new_and_legacy_routes(self):
        legacy = f'/api/manager/clients/{self.client_profile.pk}/status/'
        for key in ('manager', 'courier', 'ai_expert', self.token):
            self.assertEqual(self.patch_settings({'category': 's'}, key).status_code, 403)
            self.assertEqual(self.patch_settings({'status': 'active'}, key, legacy).status_code, 403)
        other = Distributor.objects.create(name='Other', inn='9999999999', email='other@example.com', phone='1234567')
        region = Region.objects.create(code='other', name='Other', distributor=other)
        self.client_profile.distributor = other
        self.client_profile.region = region
        self.client_profile.save()
        for url in (self.settings_url, legacy, self.url):
            self.assertEqual(self.patch_settings({'status': 'active'}, url=url).status_code, 404)
        self.assertEqual(self.patch_settings({'category': 's'}, 'admin').status_code, 200)
        self.assertEqual(self.http.get(f'/api/manager/clients/{self.client_profile.pk}/unified/', **self.auth('distributor')).status_code, 404)

    def test_validation_audit_and_distributor_unified_card(self):
        for data in ({}, [], {'status': []}, {'category': {}}, {'category': 'shop'}, {'status': 'active', 'category': 'bad'}, {'distributorId': 1}):
            self.assertEqual(self.patch_settings(data).status_code, 400, data)
        self.assertEqual(self.patch_settings({'status': 'blocked', 'category': 's'}).status_code, 200)
        self.assertEqual(AuditLog.objects.filter(user__username='distributor', model_name='ClientProfile').count(), 2)
        unified = self.http.get(f'/api/manager/clients/{self.client_profile.pk}/unified/', **self.auth('distributor'))
        self.assertEqual(unified.status_code, 200, unified.content)
        self.assertEqual(unified.json()['client']['category'], 's')

    def test_distributor_profile_validation_and_safe_identity(self):
        url = '/api/distributor/profile/'
        before = self.distributor.inn, self.distributor.regions, self.distributor.user.username
        for data in ({'inn': '0000000000'}, {'regions': []}, {'email': 'invalid'}, {'phone': 'abc1234567'}, {'phone': '1'}, {'name': ''}, {'name': 42}, {'name': 'a' * 256}, []):
            self.assertEqual(self.patch_settings(data, url=url).status_code, 400, data)
        result = self.patch_settings({'name': 'Новое название', 'email': 'new@example.com', 'phone': '+7 (900) 123-45-67'}, url=url)
        self.assertEqual(result.status_code, 200, result.content)
        self.distributor.refresh_from_db()
        self.distributor.user.refresh_from_db()
        self.assertEqual(self.distributor.user.email, 'new@example.com')
        self.assertEqual((self.distributor.inn, self.distributor.regions, self.distributor.user.username), before)
        self.assertEqual(self.http.get('/api/auth/me/', **self.auth('distributor')).json()['distributor']['name'], 'Новое название')
        for key in ('manager', 'courier', 'ai_expert', self.token):
            self.assertEqual(self.patch_settings({'name': 'Hack'}, key, url).status_code, 403)
        self.assertEqual(self.patch_settings({'name': 'Admin'}, 'admin', url + f'?distributorId={self.distributor.pk}').status_code, 200)

    def test_quote_scope_and_fresh_prices(self):
        self.set_discount('10')
        url = f'/api/products/quote/?ids={self.product.pk}'
        result = self.http.get(url, **self.auth(self.token))
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()['results'][0]['price'], 900)
        self.assertEqual(self.http.get('/api/products/quote/?ids=bad', **self.auth(self.token)).status_code, 400)
        self.assertEqual(self.http.get(url, **self.auth('distributor')).status_code, 403)
        other = Distributor.objects.create(name='Other', inn='9999999999', email='other@example.com', phone='1234567')
        self.product.distributor = other
        self.product.save()
        self.assertEqual(self.http.get(url, **self.auth(self.token)).json()['results'], [])

    def test_django_admin_scopes_and_critical_region_identity(self):
        from django.contrib import admin
        from django.contrib.auth.models import User
        from django.test import RequestFactory
        from .admin import ClientProfileAdmin, DistributorAdmin, RegionAdmin
        request = RequestFactory().get('/')
        request.user = User.objects.get(username='distributor')
        clients = ClientProfileAdmin(ClientProfile, admin.site)
        distributors = DistributorAdmin(Distributor, admin.site)
        regions = RegionAdmin(Region, admin.site)
        self.assertEqual(list(clients.get_queryset(request)), [self.client_profile])
        self.assertIn('distributor', clients.get_readonly_fields(request, self.fresh()))
        self.assertIn('region', clients.get_readonly_fields(request, self.fresh()))
        self.assertNotIn('category', clients.get_readonly_fields(request, self.fresh()))
        self.assertIn('inn', distributors.get_readonly_fields(request, self.distributor))
        self.assertIn('regions', distributors.get_readonly_fields(request, self.distributor))
        self.assertFalse(regions.has_change_permission(request, self.region))
        other = Distributor.objects.create(name='Other', inn='9999999999', email='other@example.com', phone='1234567')
        self.client_profile.distributor = other
        self.client_profile.save()
        self.assertFalse(clients.get_queryset(request).exists())
        self.assertEqual(list(distributors.get_queryset(request)), [self.distributor])
        request.user = User.objects.get(username='admin')
        self.assertEqual(clients.get_queryset(request).count(), 1)
        self.assertEqual(distributors.get_readonly_fields(request), ())

    def test_distributor_advice_only_reaches_own_client(self):
        from unittest.mock import patch
        body = {'userId': self.client_profile.user_id, 'title': 'Совет', 'body': 'Текст', 'type': 'recommendation'}
        with patch('api.services.push_notifications.PushNotificationService.send', return_value={'sent': 1}):
            response = self.http.post('/api/notifications/send/', body, content_type='application/json', **self.auth('distributor'))
        self.assertEqual(response.status_code, 200, response.content)
        other = Distributor.objects.create(name='Other', inn='9999999999', email='other@example.com', phone='1234567')
        self.client_profile.distributor = other
        self.client_profile.save()
        response = self.http.post('/api/notifications/send/', body, content_type='application/json', **self.auth('distributor'))
        self.assertEqual(response.status_code, 403)

    def test_admin_status_change_uses_same_account_lifecycle(self):
        from django.contrib import admin
        from django.contrib.auth.models import User
        from django.test import RequestFactory
        from .admin import ClientProfileAdmin
        request = RequestFactory().post('/')
        request.user = User.objects.get(username='admin')
        model_admin = ClientProfileAdmin(ClientProfile, admin.site)
        client = self.fresh()
        client.status = 'archived'
        model_admin.save_model(request, client, None, True)
        client.user.refresh_from_db()
        self.assertFalse(client.user.is_active)
        self.assertFalse(client.user.auth_tokens.exists())
        client.status = 'active'
        model_admin.save_model(request, client, None, True)
        client.user.refresh_from_db()
        self.assertTrue(client.user.is_active)
