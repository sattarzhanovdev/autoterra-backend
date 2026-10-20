"""Persistent, retryable delivery of registration notifications."""
import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import F, Q
from django.utils import timezone

from api.models import RegistrationEmailDelivery

logger = logging.getLogger(__name__)


def send_registration_email(client):
    delivery, _ = RegistrationEmailDelivery.objects.get_or_create(client=client)
    now = timezone.now()
    # Claim without holding a database transaction during SMTP. The lease also
    # lets a later worker recover an attempt interrupted by a process restart.
    claimed = RegistrationEmailDelivery.objects.filter(pk=delivery.pk, sent_at__isnull=True).filter(
        Q(locked_until__isnull=True) | Q(locked_until__lte=now)
    ).update(locked_until=now + timedelta(seconds=max(settings.EMAIL_TIMEOUT * 4, 300)), attempts=F('attempts') + 1)
    if not claimed:
        return False
    try:
        if settings.EMAIL_BACKEND in (
            'django.core.mail.backends.console.EmailBackend',
            'django.core.mail.backends.dummy.EmailBackend',
            'django.core.mail.backends.filebased.EmailBackend',
        ):
            raise RuntimeError('SMTP delivery is not configured')
        from api.views import _deliver_registration_email
        if _deliver_registration_email(client) != 1:
            raise RuntimeError('Email backend did not accept the message')
    except Exception as exc:
        # Store only the exception type: SMTP errors can include credentials or
        # personal data. Full diagnostics remain in the server's protected log.
        RegistrationEmailDelivery.objects.filter(pk=delivery.pk).update(
            locked_until=None, last_error=type(exc).__name__,
        )
        logger.exception('Registration email remains queued: client=%s', client.pk)
        return False
    RegistrationEmailDelivery.objects.filter(pk=delivery.pk).update(
        sent_at=timezone.now(), locked_until=None, last_error='',
    )
    return True
