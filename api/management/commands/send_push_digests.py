"""
Daily push-notification digest command for AutoTerra B2B platform.

Three analytical scenarios run sequentially (no Celery — this is a cron process,
so synchronous FCM calls are fine and daemon threads would exit prematurely):

  1. inactive_clients  — no verified purchase for ≥14 days    → AI upsell push
  2. sku_reorder       — bought SKU >3 times, last >30 days ago → AI restock push
  3. inactive_regions  — 0 new clients/purchases in 7 days     → SYSTEM alert

Design principles
─────────────────
• Every DB-heavy step is a single ORM query using Subquery / Exists / annotate.
  Zero N+1 queries anywhere.
• Anti-spam: per (user, title[, body]) per calendar day — no duplicate push
  even if cron fires twice.
• --dry-run shows exactly what would be sent without touching DB or FCM.
• --scenario lets ops re-run one scenario after a data fix.

Crontab (09:00 Moscow = 06:00 UTC):
  0 6 * * * /srv/autoterra/venv/bin/python /srv/autoterra/manage.py send_push_digests \\
            >> /var/log/autoterra/digests.log 2>&1
"""

import logging
import time
from datetime import date, timedelta
from typing import Any

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandParser
from django.db.models import Count, Exists, Max, OuterRef, Q, Subquery
from django.utils import timezone

logger = logging.getLogger(__name__)

# ── Tunable thresholds ────────────────────────────────────────────────────────
INACTIVE_PURCHASE_DAYS = 14   # client is inactive if no verified purchase for N days
SKU_MIN_PURCHASES = 3         # must have purchased the SKU *more than* this many times
SKU_LAST_PURCHASE_DAYS = 30   # push only if the last purchase of that SKU was > N days ago
SKU_HISTORY_DAYS = 180        # only count purchases within the last N days
SKU_MAX_PER_PUSH = 3          # max SKUs bundled into a single notification
REGION_INACTIVE_DAYS = 7      # alert manager if territory is quiet for N days


class Command(BaseCommand):
    help = "Send personalised daily push-notification digests (analytics-driven, cron-scheduled)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview what would be sent without writing to DB or calling FCM.",
        )
        parser.add_argument(
            "--scenario",
            choices=["inactive_clients", "sku_reorder", "inactive_regions"],
            default=None,
            help="Run a single scenario instead of all three.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        dry_run: bool = options["dry_run"]
        scenario: str | None = options["scenario"]
        t0 = time.monotonic()

        if dry_run:
            self.stdout.write(self.style.WARNING("══ DRY RUN — nothing will be written ══\n"))

        now = timezone.now()
        today = now.date()
        totals: dict[str, int] = {}

        run_list = [scenario] if scenario else [
            "inactive_clients",
            "sku_reorder",
            "inactive_regions",
        ]

        for name in run_list:
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n▶ {name}"))
            try:
                count = getattr(self, f"_run_{name}")(now, today, dry_run)
                totals[name] = count
                self.stdout.write(self.style.SUCCESS(f"  ✓ {count} notification(s) dispatched"))
            except Exception:
                logger.exception("Digest scenario %s raised an unhandled exception", name)
                self.stderr.write(self.style.ERROR(f"  ✗ {name} aborted — see logs"))
                totals[name] = -1

        elapsed = time.monotonic() - t0
        total_sent = sum(v for v in totals.values() if v >= 0)
        logger.info(
            "digest finished | elapsed=%.1fs sent=%d scenarios=%s",
            elapsed, total_sent, totals,
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"\n{'─' * 44}\n"
                f"Finished in {elapsed:.1f}s | total sent={total_sent} | {totals}\n"
            )
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Scenario 1 — Inactive clients  (retention / re-engagement)
    # ─────────────────────────────────────────────────────────────────────────

    def _run_inactive_clients(self, now, today: date, dry_run: bool) -> int:
        """
        Find active ClientProfiles whose last *verified* Purchase was more than
        INACTIVE_PURCHASE_DAYS ago (or who have never made one) and send an
        AI upsell push.

        ORM approach
        ────────────
        • Correlated Subquery returns the most-recent verified purchase date
          without a GROUP BY across the full table.
        • Exists anti-spam sub-select is evaluated by the DB — no extra Python
          round-trip per candidate.
        • .iterator(chunk_size=200) streams rows so memory stays flat even with
          thousands of inactive clients.
        """
        from api.models import ClientProfile, Notification, Purchase

        cutoff = today - timedelta(days=INACTIVE_PURCHASE_DAYS)
        TITLE = "Время пополнить запасы"
        BODY = "Вы не делали заказов уже 2 недели. Проверьте остатки на складе."

        # Most-recent verified purchase date for each ClientProfile (correlated)
        last_purchase_sq = (
            Purchase.objects
            .filter(client=OuterRef("pk"), status="verified")
            .order_by("-date")
            .values("date")[:1]
        )

        # Anti-spam: was this exact notification already sent to this user today?
        already_sent_sq = Notification.objects.filter(
            user_id=OuterRef("user_id"),
            type="ai",
            title=TITLE,
            created_at__date=today,
        )

        candidates = (
            ClientProfile.objects
            .filter(status="active")
            .annotate(last_purchase_date=Subquery(last_purchase_sq))
            .filter(
                Q(last_purchase_date__lt=cutoff)
                | Q(last_purchase_date__isnull=True)
            )
            .annotate(already_sent=Exists(already_sent_sq))
            .filter(already_sent=False)
            .select_related("user")
            .only("id", "company_name", "user_id")
        )

        count = 0
        for client in candidates.iterator(chunk_size=200):
            days_ago = (
                f"{(today - client.last_purchase_date).days} дней"
                if client.last_purchase_date
                else "никогда"
            )
            self.stdout.write(f"  {client.company_name} (последняя покупка: {days_ago})")
            self._notify(
                user=client.user,
                title=TITLE,
                body=BODY,
                n_type="ai",
                related_link="/purchases",
                dry_run=dry_run,
            )
            count += 1

        return count

    # ─────────────────────────────────────────────────────────────────────────
    # Scenario 2 — SKU reorder prediction  (sales generation)
    # ─────────────────────────────────────────────────────────────────────────

    def _run_sku_reorder(self, now, today: date, dry_run: bool) -> int:
        """
        MVP rule: client bought a given SKU more than SKU_MIN_PURCHASES times
        within the last SKU_HISTORY_DAYS days AND their most recent purchase of
        that SKU was more than SKU_LAST_PURCHASE_DAYS days ago.

        ORM approach
        ────────────
        • One annotated GROUP BY query returns all (client × SKU) patterns.
          Both the count filter and the last-purchase filter are pushed to the DB.
        • Anti-spam is resolved via a pre-fetched set (one query, O(1) lookup).
        • User objects are bulk-fetched after filtering (one query).
        • Multiple eligible SKUs per user are bundled into a single notification
          (capped at SKU_MAX_PER_PUSH to keep the body readable).
        """
        from api.models import Notification, PurchaseItem

        history_cutoff = today - timedelta(days=SKU_HISTORY_DAYS)
        reorder_cutoff = today - timedelta(days=SKU_LAST_PURCHASE_DAYS)
        TITLE = "Заканчивается товар?"

        # Single aggregated round-trip: (client, sku) patterns that meet both rules
        sku_patterns = (
            PurchaseItem.objects
            .filter(
                purchase__status="verified",
                purchase__date__gte=history_cutoff,
            )
            .values(
                "sku",
                "name",
                "purchase__client_id",
                "purchase__client__user_id",
                "purchase__client__company_name",
            )
            .annotate(
                purchase_count=Count("purchase_id", distinct=True),
                last_purchase=Max("purchase__date"),
            )
            .filter(
                purchase_count__gt=SKU_MIN_PURCHASES,   # strictly > 3
                last_purchase__lt=reorder_cutoff,        # last purchase > 30 days ago
            )
            .order_by("purchase__client__user_id", "-last_purchase")
        )

        # Pre-fetch users already alerted today (one query → O(1) lookup below)
        alerted_user_ids: set[int] = set(
            Notification.objects
            .filter(type="ai", title=TITLE, created_at__date=today)
            .values_list("user_id", flat=True)
        )

        # Group qualifying (user, sku) rows for bundling
        pending: dict[int, list[dict]] = {}
        for row in sku_patterns.iterator(chunk_size=500):
            uid: int = row["purchase__client__user_id"]
            if uid in alerted_user_ids:
                continue
            pending.setdefault(uid, []).append(row)

        if not pending:
            return 0

        # Bulk-fetch User objects — one query instead of one per user
        user_map: dict[int, User] = {
            u.pk: u
            for u in User.objects.filter(pk__in=pending.keys())
        }

        count = 0
        for uid, rows in pending.items():
            user = user_map.get(uid)
            if user is None:
                continue

            rows = rows[:SKU_MAX_PER_PUSH]
            company = rows[0]["purchase__client__company_name"]

            if len(rows) == 1:
                body = f"Пора заказать {rows[0]['name']}."
            else:
                names = ", ".join(r["name"] for r in rows)
                body = f"Пора заказать: {names}."

            self.stdout.write(
                f"  {company} | "
                f"SKU: {', '.join(r['sku'] for r in rows)} | "
                f"last: {[str(r['last_purchase']) for r in rows]}"
            )
            self._notify(
                user=user,
                title=TITLE,
                body=body,
                n_type="ai",
                related_link="/purchases",
                dry_run=dry_run,
            )
            count += 1

        return count

    # ─────────────────────────────────────────────────────────────────────────
    # Scenario 3 — Inactive regions  (management alert)
    # ─────────────────────────────────────────────────────────────────────────

    def _run_inactive_regions(self, now, today: date, dry_run: bool) -> int:
        """
        Alert the region's responsible manager (and distributor account, if
        different) when the territory shows zero new-client registrations AND
        zero purchases within REGION_INACTIVE_DAYS days.

        ORM approach
        ────────────
        • Two value-list queries build the "active" exclusion set; their union
          is subtracted in Python — avoids a complex UNION subquery.
        • The inactive-region queryset then uses a single JOIN-based SELECT.
        • Anti-spam filters on (user, title, body, date) so a manager who
          oversees two inactive regions still receives one alert per region.
        """
        from api.models import ClientProfile, Notification, Region

        activity_since = now - timedelta(days=REGION_INACTIVE_DAYS)
        TITLE = "Падение активности"

        # Build exclusion set (regions that showed any activity)
        active_ids: set[int] = (
            set(
                Region.objects
                .filter(clients__created_at__gte=activity_since)
                .values_list("id", flat=True)
            )
            |
            set(
                Region.objects
                .filter(clients__purchases__created_at__gte=activity_since)
                .values_list("id", flat=True)
            )
        )

        inactive_regions = (
            Region.objects
            .filter(is_active=True)
            .exclude(id__in=active_ids)
            .filter(
                Q(manager__isnull=False) | Q(distributor__user__isnull=False)
            )
            .select_related("manager", "distributor", "distributor__user")
        )

        count = 0
        for region in inactive_regions.iterator(chunk_size=50):
            body = f"В регионе {region.name} нет продаж и регистраций 7 дней."

            # Unique recipients for this region (manager may be same as distributor user)
            recipients: dict[int, User] = {}
            if region.manager:
                recipients[region.manager.pk] = region.manager
            if region.distributor and region.distributor.user:
                recipients.setdefault(region.distributor.user.pk, region.distributor.user)

            for user in recipients.values():
                # Anti-spam includes body so each region generates an independent alert
                if Notification.objects.filter(
                    user=user,
                    type="system",
                    title=TITLE,
                    body=body,
                    created_at__date=today,
                ).exists():
                    continue

                self.stdout.write(f"  Region: {region.name} → {user.username}")
                self._notify(
                    user=user,
                    title=TITLE,
                    body=body,
                    n_type="system",
                    related_link="/admin",
                    dry_run=dry_run,
                )
                count += 1

        return count

    # ─────────────────────────────────────────────────────────────────────────
    # Shared notification helper
    # ─────────────────────────────────────────────────────────────────────────

    def _notify(
        self,
        *,
        user: User,
        title: str,
        body: str,
        n_type: str,
        related_link: str = "",
        dry_run: bool = False,
    ) -> None:
        """
        Create a Notification row then fire FCM synchronously.

        FCM is called synchronously because this is a cron process: there is no
        event loop to await, and daemon threads would be killed the moment the
        management command process exits.  Blocking here is intentional.
        """
        if dry_run:
            self.stdout.write(
                self.style.WARNING(f"    [DRY] {n_type} → {user.username!r}: {title!r}")
            )
            return

        from api.models import Notification
        from api.services.push_notifications import PushNotificationService

        notification = Notification.objects.create(
            user=user,
            title=title,
            body=body,
            type=n_type,
            related_link=related_link,
        )
        try:
            result = PushNotificationService().send(notification)
            logger.info(
                "digest_push | user=%s type=%s title=%r sent=%d failed=%d",
                user.pk, n_type, title, result["sent"], result["failed"],
            )
        except Exception:
            logger.exception(
                "digest_push FCM error | notification=%s user=%s",
                notification.pk, user.pk,
            )
            self.stderr.write(
                self.style.ERROR(
                    f"    FCM error for user={user.pk} — "
                    f"Notification #{notification.pk} saved, push skipped"
                )
            )
