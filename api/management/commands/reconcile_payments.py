"""Recover delayed webhooks and creation responses lost after a network failure."""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from api.models import Order, Payment
from api.services import payments, bonuses
from api.views import _refresh_payment, _create_provider_payment


class Command(BaseCommand):
    help = "Проверить незавершённые платежи ЮKassa. Запускать каждую минуту."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        if not payments.is_configured():
            raise CommandError("Задайте YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY")
        failed = 0
        ids = list(Payment.objects.filter(provider="yookassa",
            status__in=["pending", "waiting_for_capture"]).order_by("created_at")
            .values_list("pk", flat=True)[:options["limit"]])
        for pk in ids:
            payment = Payment.objects.get(pk=pk)
            if payment.status not in ("pending", "waiting_for_capture"):
                continue
            try:
                if payment.provider_payment_id:
                    _refresh_payment(payment)
                else:
                    if ((timezone.now() - payment.created_at).total_seconds() >= 23 * 3600
                            or not payment.idempotence_key or not payment.raw_response.get("request")):
                        failed += 1
                        self.stderr.write(f"Платёж {pk}: нужна ручная сверка в ЮKassa; новая попытка не создана")
                        continue
                    _create_provider_payment(payment)
            except (payments.PaymentUncertainError, payments.PaymentConfigError):
                failed += 1
                self.stderr.write(f"Платёж {pk}: результат пока неизвестен, резерв сохранён")
            except payments.PaymentProviderError:
                failed += 1
                if not payment.provider_payment_id:
                    with transaction.atomic():
                        Order.objects.select_for_update().get(pk=payment.order_id)
                        current = Payment.objects.select_for_update().get(pk=pk)
                        if current.status == "pending" and not current.provider_payment_id:
                            current.status = "canceled"
                            current.save(update_fields=["status"])
                            bonuses.refund_for_order(current.order)
                self.stderr.write(f"Платёж {pk}: запрос отклонён провайдером")
        self.stdout.write(f"Проверено: {len(ids)}; требуют повторной проверки: {failed}")
        if failed:
            raise CommandError("Не все платежи сверены; проверьте сообщения выше")
