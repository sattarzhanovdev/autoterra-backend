from decimal import Decimal
from django.contrib.auth.models import User
from django.test import override_settings
from .models import AuthToken, ClientProfile, ClientPriceOverride, Order, PartnerTier, RankDiscount
from .test_bonus_account import _Base
from .services.pricing import price_for_client
from .serializers import RegistrationSerializer


class ShopMarkupTests(_Base):
    def setUp(self):
        super().setUp()
        self.client_profile.category = 's'
        self.client_profile.save()
        self.url = '/api/me/markup/'

    def auth(self, token=None):
        return {'HTTP_AUTHORIZATION': f'Bearer {token or self.token}'}

    def save_markup(self, value, **extra):
        return self.http.patch(self.url, {'markupPercent': value, **extra},
                               content_type='application/json', **self.auth())

    def test_default_and_save_read_back(self):
        self.assertEqual(self.http.get(self.url, **self.auth()).json()['markupPercent'], '0.00')
        self.assertEqual(self.save_markup('30').status_code, 200)
        self.assertEqual(self.http.get(self.url, **self.auth()).json()['markupPercent'], '30.00')
        self.assertEqual(self.save_markup('9999.99').status_code, 200)
        self.assertEqual(self.save_markup('0').status_code, 200)

    def test_invalid_values_and_payloads_do_not_change_preference(self):
        self.save_markup('30')
        for value in ('-1', '-0.001', 'NaN', 'Infinity', '10000', '1.001', '', None, True, {}, []):
            with self.subTest(value=value):
                self.assertEqual(self.save_markup(value).status_code, 400)
        for payload in ({}, [], {'markupPercent': 10, 'clientId': 99}):
            self.assertEqual(self.http.patch(self.url, payload, content_type='application/json', **self.auth()).status_code, 400)
        self.client_profile.refresh_from_db()
        self.assertEqual(self.client_profile.markup_percent, Decimal('30'))

    def test_only_active_shop_client_can_read_or_write(self):
        self.assertEqual(self.http.get(self.url).status_code, 401)
        for role in ('distributor', 'manager', 'admin', 'courier', 'ai_expert'):
            user = User.objects.create_user(username=role)
            user.profile.role = role
            user.profile.save()
            AuthToken.objects.create(user=user, key=role)
            for method in ('get', 'patch'):
                result = getattr(self.http, method)(self.url, **self.auth(role))
                self.assertEqual(result.status_code, 403, (role, method))
        for category in ('a', 'b', 'c'):
            self.client_profile.category = category
            self.client_profile.save()
            self.assertEqual(self.http.get(self.url, **self.auth()).status_code, 403)
            self.assertEqual(self.save_markup(10).status_code, 403)
            row = self.http.get('/api/products/', **self.auth()).json()['results'][0]
            self.assertNotIn('markupPercent', row)
        self.client_profile.category = 's'
        for status in ('new', 'under_review', 'blocked', 'archived'):
            self.client_profile.status = status
            self.client_profile.save()
            self.assertEqual(self.save_markup(10).status_code, 403)

    def test_cannot_change_another_shop(self):
        other = self._client('other-shop', '1111111111')
        other.category = 's'
        other.save()
        self.assertEqual(self.save_markup(30, clientId=other.pk).status_code, 400)
        self.assertEqual(self.save_markup(30).status_code, 200)
        other.refresh_from_db()
        self.assertEqual(other.markup_percent, 0)

    def test_final_purchase_price_and_orders_unaffected(self):
        self.client_profile.partner_status = 'Platinum'
        self.client_profile.save()
        RankDiscount.objects.create(tier=PartnerTier.objects.get(name='Platinum'), percent=10)
        for source, expected in (('rank', 900), ('personal_discount', 800), ('personal', 700)):
            if source == 'personal_discount':
                self.client_profile.personal_discount_percent = 20
                self.client_profile.save()
            elif source == 'personal':
                ClientPriceOverride.objects.create(client=self.client_profile, product=self.product, price=700)
            self.save_markup(30)
            row = self.http.get('/api/products/', **self.auth()).json()['results'][0]
            self.assertEqual(row['price'], expected)
            self.assertEqual(row['basePrice'], 1000)
            self.assertEqual(row['priceSource'], source)
            self.assertEqual(row['markupPercent'], '30.00')
            self.assertEqual(row['markupClientId'], str(self.client_profile.pk))
            response = self.http.post('/api/orders/create/', {'items': [
                {'productId': self.product.pk, 'quantity': 2, 'price': expected * 1.3}]},
                content_type='application/json', **self.auth())
            self.assertEqual(response.status_code, 201, response.content)
            order = Order.objects.get(pk=response.json()['order']['id'])
            self.assertEqual(order.items.get().price, expected)
            self.assertEqual(order.total_amount, expected * 2)
            self.save_markup(75)
            order.refresh_from_db()
            self.assertEqual(order.total_amount, expected * 2)
            self.client_profile.refresh_from_db()
            self.assertEqual(price_for_client(ClientProfile.objects.get(pk=self.client_profile.pk), self.product), expected)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_registration_validates_shop_category(self):
        payload = dict(username='new-shop', password='password123', inn='1234567890',
                       region_id=self.region.pk, company_name='Shop', contact_name='Ivan',
                       store_address='Street', email='shop@example.com', termsAccepted=True,
                       personalDataConsent=True, category='s')
        serializer = RegistrationSerializer(payload)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data['category'], 's')
        response = self.http.post('/api/register/', payload, content_type='application/json')
        self.assertEqual(response.status_code, 201, response.content)
        shop = ClientProfile.objects.get(user__username='new-shop')
        self.assertEqual(shop.category, 's')
        self.assertEqual(shop.markup_percent, 0)
        for category in ('invalid', [], None):
            serializer = RegistrationSerializer({**payload, 'category': category})
            self.assertFalse(serializer.is_valid())
