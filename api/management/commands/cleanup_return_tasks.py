"""Убирает устаревшие задачи курьера на возврат образца.

Возврат курьером отменён: готовую краску и лючок маляр забирает сам, чтобы
проверить оттенок на месте. Задачи с task_type="return", созданные до этого
решения, никому не назначены и висят в списке доставок — их надо убрать.

Использование:
    python manage.py cleanup_return_tasks              # что найдено (без записи)
    python manage.py cleanup_return_tasks --apply      # удалить
    python manage.py cleanup_return_tasks --apply --cancel   # не удалять, а отменить

По умолчанию удаляются только незавершённые задачи: доставленные оставляем как
историю. Снять это ограничение — флагом --include-finished.
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.models import CourierTask

RETURN_TYPE = "return"
FINISHED = ("delivered", "returned", "cancelled")


class Command(BaseCommand):
    help = "Удаляет (или отменяет) устаревшие задачи курьера на возврат образца"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Записать изменения. Без этого флага команда только показывает найденное.",
        )
        parser.add_argument(
            "--cancel",
            action="store_true",
            help="Не удалять, а перевести в статус «Отменено» — задачи останутся в истории.",
        )
        parser.add_argument(
            "--include-finished",
            action="store_true",
            help="Трогать и завершённые задачи (по умолчанию они сохраняются как история).",
        )

    def handle(self, *args, **options):
        qs = CourierTask.objects.filter(task_type=RETURN_TYPE)
        total = qs.count()
        if not options["include_finished"]:
            qs = qs.exclude(status__in=FINISHED)

        targets = qs.order_by("id")
        count = targets.count()

        self.stdout.write(f"Задач на возврат образца: {total}")
        self.stdout.write(f"  под очистку: {count}")
        if not options["include_finished"] and total > count:
            self.stdout.write(f"  завершённых сохраняем как историю: {total - count}")

        if not count:
            self.stdout.write(self.style.SUCCESS("Нечего убирать."))
            return

        self.stdout.write("")
        self.stdout.write("Примеры (первые 10):")
        for task in targets[:10]:
            client = task.client.company_name if task.client_id else "—"
            self.stdout.write(
                f"  #{task.id} · {client} · {task.address} · статус {task.get_status_display()}"
            )

        if not options["apply"]:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING("Пробный прогон: ничего не изменено. Повторите с флагом --apply.")
            )
            return

        if options["cancel"]:
            processed = self._cancel(targets)
            self.stdout.write(self.style.SUCCESS(f"Отменено задач: {processed}"))
        else:
            processed, _ = targets.delete()
            self.stdout.write(self.style.SUCCESS(f"Удалено записей: {processed}"))

    def _cancel(self, targets):
        """Отмена вместо удаления: задача остаётся в истории со следом в статусах."""
        now = timezone.now()
        entry = {
            "status": "cancelled",
            "at": now.isoformat(),
            "by": None,
            "comment": "Возврат образца курьером отменён — клиент забирает сам",
        }
        processed = 0
        with transaction.atomic():
            for task in targets:
                task.status = "cancelled"
                task.status_history = [*(task.status_history or []), entry]
                task.save(update_fields=["status", "status_history"])
                processed += 1
        return processed
