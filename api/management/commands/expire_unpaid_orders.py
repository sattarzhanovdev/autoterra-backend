from datetime import timedelta
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from api.models import Order, AuditLog
from api.services.stock import restore_order_stock


class Command(BaseCommand):
    help = 'Cancel expired unpaid reservations; pending provider payments must be reconciled first.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(hours=settings.ORDER_RESERVATION_TTL_HOURS)
        candidates = Order.objects.filter(
            Q(status='new', created_at__lt=cutoff) |
            Q(status='confirmed', confirmed_at__lt=cutoff) |
            Q(status='confirmed', confirmed_at__isnull=True, created_at__lt=cutoff)
        ).values_list('pk', flat=True)
        count = 0
        for pk in candidates.iterator():
            with transaction.atomic():
                order = Order.objects.select_for_update().get(pk=pk)
                since = order.confirmed_at or order.created_at
                if order.status not in ('new', 'confirmed') or since >= cutoff or (order.status == 'confirmed' and order.payment_method == 'cash'):
                    continue
                if order.payments.filter(status__in=['pending', 'waiting_for_capture', 'succeeded']).exists():
                    continue
                count += 1
                if options['dry_run']:
                    continue
                restore_order_stock(order)
                order.status = 'cancelled'
                order.rejection_reason = 'Истёк срок резервирования'
                order.save(update_fields=['status', 'rejection_reason'])
                AuditLog.objects.create(action='Order reservation expired', model_name='Order', object_id=str(order.pk))
                from api.views import _notify_client_order
                _notify_client_order(order, 'Резерв истёк', f'Заказ ORD-{order.pk:05d} отменён: истёк срок резервирования.')
        self.stdout.write(f'{"Would expire" if options["dry_run"] else "Expired"}: {count}')
