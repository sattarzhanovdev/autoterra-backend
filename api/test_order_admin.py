from decimal import Decimal
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth.models import User, Permission
from django.urls import reverse
from django.utils import timezone

from .test_bonus_account import _Base
from .models import Order, OrderItem, OrderAdjustment, Product, Payment, Store, Distributor
from .services import orders


class OrderAdminTests(_Base):
    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_superuser('order-admin', 'admin@example.com', 'pw')
        self.http.force_login(self.staff)
        self.order = self._order(1000, status='new')
        self.item = self.order.items.get()
        self.item.reserved_quantity = 1
        self.item.save(update_fields=['reserved_quantity'])
        self.product.quantity = 999
        self.product.save(update_fields=['quantity'])
        self.url = reverse('admin:api_order_change', args=[self.order.pk])
        self.items_url = reverse('admin:api_order_adjust_items', args=[self.order.pk])
        self.model_admin = admin.site._registry[Order]

    def adjust(self, *, quantity=None, delete=False, product=None, add_quantity=1):
        data = {'items-TOTAL_FORMS': '4', 'items-INITIAL_FORMS': '1',
                'items-MIN_NUM_FORMS': '0', 'items-MAX_NUM_FORMS': '500',
                'items-0-item': str(self.item.pk), 'items-0-product': str(self.product.pk),
                'items-0-quantity': str(quantity if quantity is not None else self.item.quantity),
                'reason': 'Проверка корректировки'}
        if delete:
            data['items-0-DELETE'] = 'on'
        for index in range(1, 4):
            data.update({f'items-{index}-item': '', f'items-{index}-product': '', f'items-{index}-quantity': '1'})
        if product:
            data.update({'items-1-product': str(product.pk), 'items-1-quantity': str(add_quantity)})
        return self.http.post(self.items_url, data)

    def metadata(self, **changes):
        page = self.http.get(self.url)
        form = page.context['adminform'].form
        data = {name: form[name].value() if form[name].value() is not None else '' for name in form.fields}
        for inline in page.context['inline_admin_formsets']:
            fs = inline.formset
            prefix = fs.prefix
            data.update({f'{prefix}-TOTAL_FORMS': str(fs.total_form_count()),
                         f'{prefix}-INITIAL_FORMS': str(fs.initial_form_count()),
                         f'{prefix}-MIN_NUM_FORMS': '0', f'{prefix}-MAX_NUM_FORMS': str(fs.max_num)})
            for index, row in enumerate(fs.forms):
                for name in row.fields:
                    value = row[name].value()
                    data[f'{prefix}-{index}-{name}'] = value if value is not None else ''
        data.update(changes)
        return self.http.post(self.url, data)

    def transition(self, target):
        return self.http.post(reverse('admin:api_order_transition', args=[self.order.pk, target]), {'reason': 'Admin action'})

    def new_product(self, **changes):
        values = dict(distributor=self.distributor, sku='P-2', name='Лак', category='Лаки', price=200, quantity=10)
        values.update(changes)
        return Product.objects.create(**values)

    def test_change_quantity_reserves_stock_and_records_actor_snapshots(self):
        response = self.adjust(quantity=3)
        self.assertEqual(response.status_code, 302, response.content)
        self.item.refresh_from_db()
        self.product.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual((self.item.quantity, self.item.reserved_quantity, self.product.quantity), (3, 3, 997))
        self.assertEqual(self.order.status, 'adjusted')
        history = OrderAdjustment.objects.get(order=self.order)
        self.assertEqual(history.created_by, self.staff)
        self.assertEqual(history.original_items[0]['quantity'], 1)
        self.assertEqual(history.adjusted_items[0]['quantity'], 3)

    def test_add_product_uses_service_and_personal_price(self):
        product = self.new_product()
        with patch('api.services.orders.adjust_order', wraps=orders.adjust_order) as command:
            response = self.adjust(product=product, add_quantity=2)
        self.assertEqual(response.status_code, 302, response.content)
        command.assert_called_once()
        product.refresh_from_db()
        self.assertEqual(product.quantity, 8)
        added = self.order.items.get(product=product)
        self.assertEqual(added.price, Decimal('200'))
        self.assertEqual(added.reserved_quantity, 2)
        self.assertEqual(OrderAdjustment.objects.filter(order=self.order).count(), 1)

    def test_delete_position_and_add_replacement_returns_only_reserved_stock(self):
        product = self.new_product()
        response = self.adjust(delete=True, product=product)
        self.assertEqual(response.status_code, 302, response.content)
        self.assertFalse(OrderItem.objects.filter(pk=self.item.pk).exists())
        self.product.refresh_from_db()
        product.refresh_from_db()
        self.assertEqual((self.product.quantity, product.quantity), (1000, 9))
        history = OrderAdjustment.objects.get(order=self.order)
        self.assertEqual(history.original_items[0]['productId'], str(self.product.pk))
        self.assertEqual(history.adjusted_items[0]['productId'], str(product.pk))

    def test_insufficient_stock_rolls_back_every_change(self):
        product = self.new_product(quantity=0, status='outOfStock')
        response = self.adjust(delete=True, product=product)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Недостаточно')
        self.assertTrue(OrderItem.objects.filter(pk=self.item.pk).exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, 999)
        self.assertFalse(OrderAdjustment.objects.exists())

    def test_cannot_remove_last_position(self):
        response = self.adjust(delete=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'не осталось позиций')
        self.assertTrue(OrderItem.objects.filter(pk=self.item.pk).exists())
        self.assertFalse(OrderAdjustment.objects.exists())

    def test_on_order_does_not_return_unreserved_units(self):
        self.product.status = 'onOrder'
        self.product.quantity = 0
        self.product.save()
        self.item.quantity = 5
        self.item.save(update_fields=['quantity'])
        response = self.adjust(quantity=3)
        self.assertEqual(response.status_code, 302)
        self.product.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual((self.product.quantity, self.item.reserved_quantity), (0, 1))

    def test_paid_shipped_and_fulfilled_are_readonly_even_for_forged_post(self):
        for status in ('paid', 'shipped', 'fulfilled'):
            with self.subTest(status=status):
                self.order.status = status
                self.order.paid_at = timezone.now()
                self.order.save()
                page = self.http.get(self.url)
                self.assertEqual(page.status_code, 200)
                self.assertNotIn('client', page.context['adminform'].form.fields)
                self.assertNotIn('status', page.context['adminform'].form.fields)
                self.assertFalse(page.context['can_adjust_items'])
                response = self.adjust(quantity=500)
                self.assertEqual(response.status_code, 302)
                self.metadata(client=self.client_profile.pk, status='new', comment='forged')
                self.order.refresh_from_db()
                self.item.refresh_from_db()
                self.assertEqual(self.order.status, status)
                self.assertEqual(self.item.quantity, 1)
                self.assertEqual(self.order.comment, '')
        self.assertFalse(OrderAdjustment.objects.exists())

    def test_pending_or_succeeded_payment_blocks_composition(self):
        for status in ('pending', 'waiting_for_capture', 'succeeded'):
            payment = Payment.objects.create(order=self.order, amount=1000, status=status)
            with self.subTest(status=status):
                self.assertEqual(self.adjust(quantity=2).status_code, 302)
                with self.assertRaises(orders.OrderError):
                    orders.adjust_order(self.order.pk, {'items': [{'itemId': self.item.pk, 'quantity': 2}]}, actor=self.staff)
            payment.delete()
        self.assertFalse(OrderAdjustment.objects.exists())

    def test_main_inline_is_readonly_and_cannot_bypass_service(self):
        response = self.metadata(**{'items-0-quantity': '500', 'status': 'paid', 'stock_restored': 'on'})
        self.assertEqual(response.status_code, 302, response.content)
        self.item.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.item.quantity, 1)
        self.assertEqual(self.order.status, 'new')
        self.assertFalse(self.order.stock_restored)
        self.assertFalse(OrderAdjustment.objects.exists())

    def test_change_store_comment_and_date(self):
        store = Store.objects.create(client=self.client_profile, name='Точка', address='Адрес')
        response = self.metadata(store=store.pk, comment='Новый комментарий', estimated_delivery_date='2026-11-01')
        self.assertEqual(response.status_code, 302, response.content)
        self.order.refresh_from_db()
        self.assertEqual(self.order.store, store)
        self.assertEqual(self.order.comment, 'Новый комментарий')
        self.assertEqual(self.order.estimated_delivery_date.isoformat(), '2026-11-01')

    def test_foreign_store_rejected(self):
        other = self._client('other-client', '1234567890')
        store = Store.objects.create(client=other, name='Чужая точка', address='Чужой адрес')
        with self.assertRaises(orders.OrderError):
            orders.update_metadata(self.order.pk, {'store': store}, actor=self.staff)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.store_id)

    def test_change_client_reprices_and_records_history(self):
        other = self._client('other-client', '1234567890')
        store = Store.objects.create(client=other, name='Новая точка', address='Адрес')
        other.personal_discount_percent = Decimal('20')
        other.save()
        response = self.metadata(client=other.pk, store=store.pk, adjustment_reason='Смена клиента')
        self.assertEqual(response.status_code, 302, response.content)
        self.order.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual((self.order.client_id, self.order.store_id), (other.pk, store.pk))
        self.assertEqual(self.item.price, Decimal('800'))
        self.assertEqual(self.order.status, 'adjusted')
        history = OrderAdjustment.objects.get(order=self.order)
        self.assertEqual(history.original_items[0]['price'], 1000)
        self.assertEqual(history.adjusted_items[0]['price'], 800)
        self.assertEqual(history.created_by, self.staff)

    def test_distributor_change_requires_new_order_and_compatible_products_client(self):
        other = Distributor.objects.create(name='Другой', inn='901234567890')
        client = self._client('other-client', '1234567890')
        client.distributor = other
        client.save()
        changes = {'client': client, 'distributor': other}
        with self.assertRaises(orders.OrderError):
            orders.update_metadata(self.order.pk, changes, actor=self.staff)
        self.product.distributor = other
        self.product.save(update_fields=['distributor'])
        updated = orders.update_metadata(self.order.pk, changes, actor=self.staff)
        self.assertEqual(updated.distributor_id, other.pk)
        self.assertEqual(updated.client_id, client.pk)
        self.assertEqual(updated.items.get().product_id, self.product.pk)
        self.order.status = 'confirmed'
        self.order.save(update_fields=['status'])
        with self.assertRaises(orders.OrderError):
            orders.update_metadata(self.order.pk, {'distributor': self.distributor}, actor=self.staff)

    def test_client_from_other_distributor_rejected(self):
        other = self._client('other-client', '1234567890')
        other.distributor = Distributor.objects.create(name='Чужой', inn='901234567890')
        other.save()
        with self.assertRaises(orders.OrderError):
            orders.update_metadata(self.order.pk, {'client': other}, actor=self.staff)

    def test_payment_method_blocked_after_any_payment_even_canceled(self):
        self.client_profile.cash_payment_allowed = True
        self.client_profile.save(update_fields=['cash_payment_allowed'])
        orders.update_metadata(self.order.pk, {'payment_method': 'cash'}, actor=self.staff)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_method, 'cash')
        Payment.objects.create(order=self.order, amount=1000, status='canceled')
        with self.assertRaises(orders.OrderError):
            orders.update_metadata(self.order.pk, {'payment_method': 'online'}, actor=self.staff)

    def test_invalid_courier_and_cash_pickup_rejected(self):
        with self.assertRaises(orders.OrderError):
            orders.update_metadata(self.order.pk, {'courier': self.staff}, actor=self.staff)
        self.client_profile.cash_payment_allowed = True
        self.client_profile.save(update_fields=['cash_payment_allowed'])
        with self.assertRaises(orders.OrderError):
            orders.update_metadata(self.order.pk, {'payment_method': 'cash', 'delivery_method': 'self_pickup'}, actor=self.staff)

    def test_status_buttons_confirm_ship_fulfill_and_forbid_marking_paid(self):
        self.assertEqual(self.transition('confirmed').status_code, 302)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'confirmed')
        self.assertIsNotNone(self.order.confirmed_at)
        self.assertEqual(self.transition('shipped').status_code, 200)
        self.assertEqual(self.transition('paid').status_code, 403)
        self.order.status = 'paid'
        self.order.paid_at = timezone.now()
        self.order.save()
        self.assertEqual(self.transition('shipped').status_code, 302)
        self.assertEqual(self.transition('fulfilled').status_code, 302)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'fulfilled')
        self.assertIsNotNone(self.order.shipped_at)

    def test_admin_cannot_accept_adjustment_instead_of_client(self):
        self.order.status = 'adjusted'
        self.order.save(update_fields=['status'])
        self.assertEqual(self.transition('confirmed').status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'adjusted')

    def test_reject_returns_stock_once_and_actions_use_transition_service(self):
        self.assertEqual(self.transition('rejected').status_code, 302)
        self.assertEqual(self.transition('rejected').status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, 1000)
        self.order.refresh_from_db()
        self.assertTrue(self.order.stock_restored)

    def test_confirmed_adjustment_clears_confirmation_and_pauses_cash_delivery(self):
        self.client_profile.cash_payment_allowed = True
        self.client_profile.save(update_fields=['cash_payment_allowed'])
        orders.update_metadata(self.order.pk, {'payment_method': 'cash'}, actor=self.staff)
        orders.transition_order(self.order.pk, 'confirmed', actor=self.staff)
        task = self.order.courier_tasks.get()
        self.assertEqual(self.adjust(quantity=2).status_code, 302)
        self.order.refresh_from_db()
        task.refresh_from_db()
        self.assertIsNone(self.order.confirmed_at)
        self.assertEqual(task.status, 'cancelled')
        self.assertEqual(task.status_history[-1]['by'], str(self.staff.pk))
        orders.transition_order(self.order.pk, 'confirmed', actor=self.client_profile.user, source='client')
        task.refresh_from_db()
        self.assertEqual(task.status, 'created')

    def test_store_options_follow_selected_client(self):
        other = self._client('other-client', '1234567890')
        store = Store.objects.create(client=other, name='Точка другого клиента', address='Адрес')
        response = self.http.get(reverse('admin:api_order_stores', args=[self.order.pk]), {'client': other.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['stores'], [{'id': store.pk, 'name': store.name}])

    def test_staff_without_order_change_permission_cannot_mutate(self):
        user = User.objects.create_user('readonly-staff', is_staff=True)
        user.profile.role = 'admin'
        user.profile.save()
        user.user_permissions.add(Permission.objects.get(codename='view_order'))
        self.http.force_login(user)
        self.assertEqual(self.http.post(self.items_url, {}).status_code, 403)
        self.assertEqual(self.transition('confirmed').status_code, 403)
        self.assertFalse(OrderAdjustment.objects.exists())

    def test_system_fields_rejected_by_service(self):
        with self.assertRaises(orders.OrderError):
            orders.update_metadata(self.order.pk, {'status': 'paid'}, actor=self.staff)


    def test_status_selector_is_visible_and_lists_only_allowed_transitions(self):
        response = self.http.get(self.url)
        self.assertContains(response, 'Изменить статус заказа')
        form = response.context['order_status_form']
        self.assertEqual(dict(form.fields['status'].choices), {
            'confirmed': 'Подтверждён', 'rejected': 'Отклонён', 'cancelled': 'Отменён'})
        self.assertContains(response, reverse('admin:api_order_set_status', args=[self.order.pk]))

    def test_status_selector_post_uses_business_service(self):
        url = reverse('admin:api_order_set_status', args=[self.order.pk])
        with patch('api.services.orders.transition_order', wraps=orders.transition_order) as command:
            response = self.http.post(url, {'status': 'confirmed', 'reason': 'Наличие проверено'})
        self.assertEqual(response.status_code, 302)
        command.assert_called_once()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'confirmed')
        self.assertIsNotNone(self.order.confirmed_at)

    def test_status_selector_rejects_arbitrary_paid_and_active_payment(self):
        url = reverse('admin:api_order_set_status', args=[self.order.pk])
        response = self.http.post(url, {'status': 'paid'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].errors)
        Payment.objects.create(order=self.order, amount=1000, status='pending')
        response = self.http.post(url, {'status': 'confirmed'})
        self.assertTrue(response.context['form'].errors)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'new')

    def test_status_selector_for_paid_order_allows_shipping(self):
        self.order.status = 'paid'
        self.order.paid_at = timezone.now()
        self.order.save()
        response = self.http.get(self.url)
        self.assertEqual(list(response.context['order_status_form'].fields['status'].choices), [('shipped', 'Отправлен')])
        response = self.http.post(reverse('admin:api_order_set_status', args=[self.order.pk]), {'status': 'shipped'})
        self.assertEqual(response.status_code, 302)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'shipped')

    def test_status_selector_requires_post_and_csrf(self):
        from django.test import Client
        url = reverse('admin:api_order_set_status', args=[self.order.pk])
        self.assertEqual(self.http.get(url).status_code, 405)
        checked = Client(enforce_csrf_checks=True)
        checked.force_login(self.staff)
        self.assertEqual(checked.post(url, {'status': 'confirmed'}).status_code, 403)
