import logging
from typing import TYPE_CHECKING

import firebase_admin
from firebase_admin import credentials, messaging
from django.conf import settings

if TYPE_CHECKING:
    from api.models import Notification

logger = logging.getLogger(__name__)

_firebase_app = None


def _get_firebase_app() -> firebase_admin.App:
    """Lazily initialise the Firebase Admin SDK singleton."""
    global _firebase_app
    if _firebase_app is None:
        cred = credentials.Certificate(settings.FCM_SERVICE_ACCOUNT_FILE)
        _firebase_app = firebase_admin.initialize_app(cred)
    return _firebase_app


class PushNotificationService:
    """Send FCM push notifications for a Notification instance."""

    def send(self, notification: "Notification") -> dict:
        """
        Deliver a push to every registered device of notification.user.
        Returns a summary dict: {"sent": int, "failed": int}.
        """
        from api.models import UserDeviceToken  # local import avoids circular

        tokens = list(
            UserDeviceToken.objects.filter(user=notification.user).values_list(
                "token", flat=True
            )
        )
        if not tokens:
            return {"sent": 0, "failed": 0}

        _get_firebase_app()

        message = messaging.MulticastMessage(
            tokens=tokens,
            notification=messaging.Notification(
                title=notification.title,
                body=notification.body,
            ),
            data={
                "type": notification.type,
                "relatedLink": notification.related_link or "",
                "notificationId": str(notification.pk),
            },
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    sound="default",
                    channel_id="autoterra_default",
                ),
            ),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(sound="default", badge=1),
                ),
            ),
        )

        response = messaging.send_each_for_multicast(message)
        self._cleanup_invalid_tokens(tokens, response)

        result = {"sent": response.success_count, "failed": response.failure_count}
        logger.info(
            "FCM multicast for user=%s: sent=%d failed=%d",
            notification.user_id,
            result["sent"],
            result["failed"],
        )
        return result

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _cleanup_invalid_tokens(self, tokens: list[str], response) -> None:
        """Remove permanently-invalid FCM tokens from the DB."""
        from api.models import UserDeviceToken

        UNRECOVERABLE = {
            "registration-token-not-registered",
            "invalid-registration-token",
        }
        invalid = [
            tokens[i]
            for i, resp in enumerate(response.responses)
            if not resp.success
            and resp.exception
            and getattr(resp.exception, "code", None) in UNRECOVERABLE
        ]
        if invalid:
            deleted, _ = UserDeviceToken.objects.filter(token__in=invalid).delete()
            logger.info("Removed %d stale FCM tokens", deleted)
