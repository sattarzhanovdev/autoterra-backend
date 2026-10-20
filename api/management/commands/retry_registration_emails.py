"""Run every minute to deliver registration emails after temporary failures."""
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date

from api.models import ClientProfile, RegistrationEmailDelivery
from api.services.registration_emails import send_registration_email


class Command(BaseCommand):
    help = 'Retry unsent registration emails; optionally queue historical app registrations'

    def add_arguments(self, parser):
        parser.add_argument('--enqueue-since', help='Queue missing app registrations from YYYY-MM-DD (inclusive)')
        parser.add_argument('--limit', type=int, default=100)

    def handle(self, *args, **options):
        from django.core.management.base import CommandError
        if options['limit'] < 1:
            raise CommandError('--limit must be positive')
        if options['enqueue_since']:
            since = parse_date(options['enqueue_since'])
            if since is None:
                raise CommandError('--enqueue-since must be YYYY-MM-DD')
            for client in ClientProfile.objects.filter(registration_source='app', created_at__date__gte=since):
                RegistrationEmailDelivery.objects.get_or_create(client=client)
        pending = RegistrationEmailDelivery.objects.filter(sent_at__isnull=True).filter(
            Q(locked_until__isnull=True) | Q(locked_until__lte=timezone.now())
        ).select_related('client__user', 'client__region', 'client__distributor').order_by('pk')[:options['limit']]
        sent = sum(send_registration_email(item.client) for item in pending)
        remaining = RegistrationEmailDelivery.objects.filter(sent_at__isnull=True).count()
        self.stdout.write(f'Sent: {sent}; pending: {remaining}')
