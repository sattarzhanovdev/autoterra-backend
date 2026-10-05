"""Manager scope, client history and safe account removal."""
from django.contrib.auth.models import User
from django.test import override_settings

from .models import AuthToken, ClientProfile, Region
from .test_bonus_account import _Base


@override_settings(SECURE_SSL_REDIRECT=False)
class ManagerClientLifecycleTests(_Base):
    def setUp(self):
        super().setUp()
        self.manager = User.objects.create_user('manager', password='pw')
        self.manager.profile.role = 'manager'
        self.manager.profile.save(update_fields=['role'])
        self.region.manager = self.manager
        self.region.save(update_fields=['manager'])
        self.auth = {'HTTP_AUTHORIZATION': 'Bearer manager-token'}
        AuthToken.objects.create(user=self.manager, key='manager-token')

    def test_dashboard_client_orders_stats_and_region_scope(self):
        order = self._order(1200)
        order.status = 'paid'
        order.save(update_fields=['status'])
        dashboard = self.http.get('/api/manager/dashboard/', **self.auth).json()
        self.assertEqual(dashboard['totalClients'], 1)
        self.assertEqual(dashboard['totalOrders'], 1)
        detail = self.http.get(f'/api/manager/clients/{self.client_profile.pk}/unified/', **self.auth).json()
        self.assertEqual(detail['stats']['orderCount'], 1)
        self.assertEqual(detail['stats']['paidOrderCount'], 1)
        self.assertEqual(detail['stats']['orderTurnover'], 1200.0)
        orders = self.http.get(f'/api/manager/clients/{self.client_profile.pk}/orders/', **self.auth).json()['results']
        self.assertEqual(orders[0]['items'][0]['name'], 'Краска')
        self.assertEqual(orders[0]['status'], 'paid')
        self.assertEqual(self.http.get('/api/manager/orders/', **self.auth).json()['count'], 1)

        foreign = Region.objects.create(code='99', name='Другой', distributor=self.distributor)
        self.client_profile.region = foreign
        self.client_profile.save(update_fields=['region'])
        self.assertEqual(self.http.get('/api/manager/dashboard/', **self.auth).json()['totalClients'], 0)
        self.assertEqual(self.http.get(f'/api/manager/clients/{self.client_profile.pk}/orders/', **self.auth).status_code, 404)
        self.assertEqual(self.http.delete(f'/api/manager/clients/{self.client_profile.pk}/remove/',
                                          {'confirmName': self.client_profile.company_name},
                                          content_type='application/json', **self.auth).status_code, 404)
        admin = User.objects.create_superuser('central-admin', password='pw')
        AuthToken.objects.create(user=admin, key='admin-token')
        admin_auth = {'HTTP_AUTHORIZATION': 'Bearer admin-token'}
        self.assertEqual(self.http.get('/api/manager/dashboard/', **admin_auth).json()['totalClients'], 1)
        self.assertEqual(self.http.get(f'/api/manager/clients/{self.client_profile.pk}/orders/', **admin_auth).status_code, 200)

    def test_empty_account_is_deleted_with_user(self):
        client = self._client('empty', '2223334445')
        path = f'/api/manager/clients/{client.pk}/remove/'
        self.assertEqual(self.http.delete(path, {'confirmName': 'wrong'}, content_type='application/json', **self.auth).status_code, 400)
        self.assertEqual(self.http.delete(path, {'confirmName': client.company_name}, content_type='application/json', **self.auth).status_code, 200)
        self.assertFalse(ClientProfile.objects.filter(pk=client.pk).exists())
        self.assertFalse(User.objects.filter(pk=client.user_id).exists())

    def test_account_with_history_requires_archive_and_preserves_order(self):
        order = self._order(1200)
        path = f'/api/manager/clients/{self.client_profile.pk}/remove/'
        response = self.http.delete(path, {'confirmName': self.client_profile.company_name},
                                    content_type='application/json', **self.auth)
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json()['canArchive'])
        self.assertEqual(self.http.post(path, {'confirmName': self.client_profile.company_name},
                                        content_type='application/json', **self.auth).status_code, 200)
        self.client_profile.refresh_from_db()
        self.assertEqual(self.client_profile.status, 'archived')
        self.client_profile.user.refresh_from_db()
        self.assertFalse(self.client_profile.user.is_active)
        self.assertEqual(self.client_profile.orders.get().pk, order.pk)
        self.assertEqual(self.http.get('/api/manager/clients/', **self.auth).json()['count'], 0)
        self.assertEqual(self.http.get('/api/manager/clients/?status=archived', **self.auth).json()['count'], 1)
