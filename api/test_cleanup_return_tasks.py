from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from .models import ClientProfile, CourierTask, Distributor, Region


class CleanupReturnTasksTests(TestCase):
    def setUp(self):
        distributor = Distributor.objects.create(
            name="Dist", inn="1112223334", phone="1", email="d@e.co"
        )
        region = Region.objects.create(code="77", name="Msk", distributor=distributor)
        user = User.objects.create_user(username="+79001110000", password="pw")
        self.client_profile = ClientProfile.objects.create(
            user=user, inn="5556667778", company_name="Автосервис",
            contact_name="Иван", phone="+79001110000",
            region=region, city="Москва", distributor=distributor,
        )

    def _task(self, task_type="return", status="created"):
        return CourierTask.objects.create(
            client=self.client_profile, task_type=task_type,
            address="Москва, пр. Мира 22", time_slot="10:00 - 18:00", status=status,
        )

    def _run(self, *args):
        out = StringIO()
        call_command("cleanup_return_tasks", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_changes_nothing(self):
        task = self._task()
        output = self._run()
        self.assertIn("Пробный прогон", output)
        self.assertTrue(CourierTask.objects.filter(id=task.id).exists())

    def test_deletes_active_return_tasks(self):
        task = self._task()
        self._run("--apply")
        self.assertFalse(CourierTask.objects.filter(id=task.id).exists())

    def test_keeps_other_task_types(self):
        delivery = self._task(task_type="delivery")
        pickup = self._task(task_type="color_lab_pickup")
        self._run("--apply")
        self.assertTrue(CourierTask.objects.filter(id=delivery.id).exists())
        self.assertTrue(CourierTask.objects.filter(id=pickup.id).exists())

    def test_finished_returns_kept_as_history(self):
        done = self._task(status="delivered")
        self._run("--apply")
        self.assertTrue(CourierTask.objects.filter(id=done.id).exists())

    def test_include_finished_removes_everything(self):
        done = self._task(status="delivered")
        self._run("--apply", "--include-finished")
        self.assertFalse(CourierTask.objects.filter(id=done.id).exists())

    def test_cancel_mode_keeps_record(self):
        task = self._task()
        self._run("--apply", "--cancel")
        task.refresh_from_db()
        self.assertEqual(task.status, "cancelled")
        self.assertEqual(task.status_history[-1]["status"], "cancelled")

    def test_nothing_to_clean(self):
        self.assertIn("Нечего убирать", self._run("--apply"))
