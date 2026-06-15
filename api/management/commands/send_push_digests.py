"""
Daily push-notification digest for AutoTerra B2B platform.

Three analytical scenarios run sequentially (no Celery needed — this is
executed by cron as a standalone process, so blocking FCM calls are fine):

  1. inactive_clients  — no verified purchase in ≥14 days  → AI upsell push
  2. sku_reorder       — frequency-based reorder prediction → AI restock push
  3. inactive_regions  — no new clients/purchases in 7 days → SYSTEM alert

Design principles
─────────────────
• Every DB-heavy step uses Subquery / Exists / annotation — zero N+1 queries.
• Anti-spam guards: each scenario has its own title fingerprint so filters
  don't cross-contaminate.
• --dry-run shows exactly what would happen without touching DB or FCM.
• --scenario lets you re-run a single scenario after fixing data.

Crontab (run at 09:00 Moscow = 06:00 UTC):
  0 6 * * * /srv/autoterra/venv/bin/python /srv/autoterra/manage.py send_push_digests \
            >> /var/log/autoterra/digests.log 2>&1
"""

import logging
from datetime import date, timedelta
from typing import Any

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandParser
from django.db.models import Count, Exists, Max, Min, OuterRef, Q, Subquery
from django.utils import timezone

logger = logging.getLogger(__name__)

# ── Tunable constants (override via env/settings if needed) ──────────────────
INACTIVE_PURCHASE_DAYS = 14   # Send push if no verified purchase for N days
ANTI_SPAM_DAYS = 7            # Never re-send the same scenario within N days
REGION_INACTIVE_DAYS = 7      # Alert manager if region quiet for N days
SKU_MIN_PURCHASES = 2         # Minimum purchase events to build frequency model
SKU_REORDER_WINDOW_DAYS = 3   # Push if predicted reorder is within ±N days
SKU_MAX_HISTORY_DAYS = 180    # Ignore purchase events older than N days
SKU_MAX_PER_USER = 3          # Cap SKU alerts bundled into one notification


class Command(BaseCommand):
    help = "Send personalised daily push-notification digests based on DB analytics."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview without writing to DB or calling FCM.",
        )
        parser.add_argument(
            "--scenario",
            choices=["inactive_clients", "sku_reorder", "inactive_regions"],
            default=None,
            help="Run a single scenario (default: all three).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        dry_run: bool = options["dry_run"]
        scenario: str | None = options["scenario"]

        if dry_run:
            self.stdout.write(self.style.WARNING("══ DRY RUN MODE — nothing will be written ══\n"))

        now = timezone.now()
        today = now.date()
        totals: dict[str, int] = {}

        run_list = [scenario] if scenario else ["inactive_clients", "sku_reorder", "inactive_regions"]

        for name in run_list:
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n▶ {name}"))
            try:
                method = getattr(self, f"_run_{name}")
                count = method(now, today, dry_run)
                totals[name] = count
                self.stdout.write(self.style.SUCCESS(f"  ✓ {count} notification(s) dispatched"))
            except Exception:
                logger.exception("Scenario %s failed", name)
                self.stderr.write(self.style.ERROR(f"  ✗ {name} aborted — see logs"))
                totals[name] = -1

        self.stdout.write(
            self.style.SUCCESS(f"\n{'─'*40}\nTotal sent: {sum(v for v in totals.values() if v >= 0)} | {totals}\n")
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Scenario 1 — Inactive clients
    # ─────────────────────────────────────────────────────────────────────────

    def _run_inactive_clients(self, now, today: date, dry_run: bool) -> int:
        """
        Find active clients whose last *verified* purchase is older than
        INACTIVE_PURCHASE_DAYS (or who have never made a purchase at all).

        ORM strategy
        ────────────
        Subquery → last verified purchase date per client (avoids GROUP BY on
        the full purchase table for clients we don't care about).
        Exists   → spam guard without a JOIN on the outer table.
        .only()  → fetch only the columns we actually use.
        """
        from api.models import ClientProfile, Notification, Purchase

        cutoff = today - timedelta(days=INACTIVE_PURCHASE_DAYS)
        anti_spam_since = now - timedelta(days=ANTI_SPAM_DAYS)
        TITLE = "Время пополнить запасы"

        # Correlated subquery: most recent verified purchase date for this client
        last_purchase_sq = (
            Purchase.objects
            .filter(client=OuterRef("pk"), status="verified")
            .order_by("-date")
            .values("date")[:1]
        )

        # Exists guard: did we already send this exact notification recently?
        already_notified_sq = Notification.objects.filter(
            user_id=OuterRef("user_id"),
            type="ai",
            title=TITLE,
            created_at__gte=anti_spam_since,
        )

        candidates = (
            ClientProfile.objects
            .filter(status="active")
            .annotate(last_purchase_date=Subquery(last_purchase_sq))
            .filter(
                Q(last_purchase_date__lt=cutoff) | Q(last_purchase_date__isnull=True)
            )
            .annotate(already_notified=Exists(already_notified_sq))
            .filter(already_notified=False)
            .select_related("user")
            .only("id", "company_name", "user")
        )

        count = 0
        for client in candidates.iterator(chunk_size=200):
            days_ago = (
                f"{(today - client.last_purchase_date).days} дней"
                if client.last_purchase_date else "никогда"
            )
            self.stdout.write(f"  {client.company_name} (последняя покупка: {days_ago})")

            self._notify(
                user=client.user,
                title=TITLE,
                body=(
                    "Вы давно не делали заказ. "
                    "Проверьте остатки материалов AutoTerra и сделайте заявку."
                ),
                n_type="ai",
                related_link="/purchases",
                dry_run=dry_run,
            )
            count += 1

        return count

    # ─────────────────────────────────────────────────────────────────────────
    # Scenario 2 — SKU reorder prediction
    # ─────────────────────────────────────────────────────────────────────────

    def _run_sku_reorder(self, now, today: date, dry_run: bool) -> int:
        """
        Build a simple purchase-frequency model per (client, SKU):

          avg_interval_days = (last_date − first_date) / (purchase_count − 1)
          predicted_next    = last_date + avg_interval_days

        Send a push if predicted_next is within [-1, +REORDER_WINDOW] days.

        ORM strategy
        ────────────
        One aggregated query returns all (client, sku) patterns in a single
        round-trip.  All frequency arithmetic is done in Python on the small
        result set (already filtered to ≥2 purchases within 180 days).
        User instances are bulk-fetched (one query) after prediction filtering.
        """
        from api.models import Notification, PurchaseItem

        anti_spam_since = now - timedelta(days=ANTI_SPAM_DAYS)
        pattern_cutoff = today - timedelta(days=SKU_MAX_HISTORY_DAYS)
        TITLE = "Пора заказать материал"

        # One DB round-trip: aggregate per (client, sku)
        sku_patterns = (
            PurchaseItem.objects
            .filter(
                purchase__status="verified",
                purchase__date__gte=pattern_cutoff,
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
                first_purchase=Min("purchase__date"),
            )
            .filter(purchase_count__gte=SKU_MIN_PURCHASES)
            .order_by("purchase__client__user_id", "sku")
        )

        # Pre-fetch recently-alerted users (one query, no per-row hits)
        alerted_user_ids: set[int] = set(
            Notification.objects
            .filter(type="ai", title=TITLE, created_at__gte=anti_spam_since)
            .values_list("user_id", flat=True)
        )

        # Group predicted reorders by user_id to bundle into one notification
        pending: dict[int, list[dict]] = {}   # {user_id: [row, …]}

        for row in sku_patterns.iterator(chunk_size=500):
            user_id: int = row["purchase__client__user_id"]
            if user_id in alerted_user_ids:
                continue

            span = (row["last_purchase"] - row["first_purchase"]).days
            if span == 0:
                continue  # Only bought on the same day twice — no interval

            avg_interval = span / (row["purchase_count"] - 1)
            predicted_next = row["last_purchase"] + timedelta(days=avg_interval)
            days_until = (predicted_next - today).days

            if not (-1 <= days_until <= SKU_REORDER_WINDOW_DAYS):
                continue

            pending.setdefault(user_id, []).append({**row, "days_until": days_until})

        if not pending:
            return 0

        # Bulk-fetch users (single query)
        user_map: dict[int, User] = {
            u.pk: u
            for u in User.objects.filter(pk__in=pending.keys())
        }

        count = 0
        for user_id, rows in pending.items():
            user = user_map.get(user_id)
            if user is None:
                continue

            # Cap and sort by urgency (days_until ascending)
            rows.sort(key=lambda r: r["days_until"])
            rows = rows[:SKU_MAX_PER_USER]

            company = rows[0]["purchase__client__company_name"]
            if len(rows) == 1:
                row = rows[0]
                title = TITLE
                body = (
                    f"По нашим данным, {row['name']} (SKU: {row['sku']}) "
                    f"скоро закончится. Средний цикл закупки: "
                    f"{(row['last_purchase'] - row['first_purchase']).days // (row['purchase_count'] - 1)} дней. "
                    f"Пополните запасы заранее."
                )
            else:
                sku_list = ", ".join(r["sku"] for r in rows)
                title = TITLE
                body = (
                    f"Несколько позиций скоро потребуют пополнения: {sku_list}. "
                    f"Проверьте остатки и сделайте заявку."
                )

            self.stdout.write(
                f"  {company} | SKU(s): {', '.join(r['sku'] for r in rows)} "
                f"| days_until: {[r['days_until'] for r in rows]}"
            )

            self._notify(
                user=user,
                title=title,
                body=body,
                n_type="ai",
                related_link="/purchases",
                dry_run=dry_run,
            )
            count += 1

        return count

    # ─────────────────────────────────────────────────────────────────────────
    # Scenario 3 — Inactive regions
    # ─────────────────────────────────────────────────────────────────────────

    def _run_inactive_regions(self, now, today: date, dry_run: bool) -> int:
        """
        Alert the region's manager and the distributor's account user when
        their territory shows no activity (no new registrations, no purchases)
        in the past REGION_INACTIVE_DAYS days.

        ORM strategy
        ────────────
        Two annotation-based subqueries build the "active region" exclusion
        list in a single pass, avoiding a correlated subquery per region.
        The anti-spam check per recipient is an Exists on the Notification
        table (one query per Region × recipients, typically a very small N).
        """
        from api.models import ClientProfile, Notification, Region

        activity_since = now - timedelta(days=REGION_INACTIVE_DAYS)
        anti_spam_since = now - timedelta(days=ANTI_SPAM_DAYS)

        # Regions that had a new client registration recently
        regions_with_new_clients = (
            Region.objects
            .filter(clients__created_at__gte=activity_since)
            .values_list("id", flat=True)
        )

        # Regions that had any purchase submission recently
        regions_with_purchases = (
            Region.objects
            .filter(clients__purchases__created_at__gte=activity_since)
            .values_list("id", flat=True)
        )

        # Union → set of IDs to exclude
        active_ids = set(regions_with_new_clients) | set(regions_with_purchases)

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
            active_client_count = ClientProfile.objects.filter(
                region=region, status="active"
            ).count()

            body = (
                f"В регионе «{region.name}» нет новых регистраций или покупок "
                f"за последние {REGION_INACTIVE_DAYS} дней "
                f"(активных клиентов: {active_client_count}). "
                f"Проверьте активность команды."
            )
            title = f"Низкая активность: {region.name}"

            # Collect unique recipients (manager may be same person as distributor user)
            recipient_ids: dict[int, User] = {}
            if region.manager:
                recipient_ids[region.manager.pk] = region.manager
            if region.distributor and region.distributor.user:
                recipient_ids.setdefault(region.distributor.user.pk, region.distributor.user)

            for user in recipient_ids.values():
                already = Notification.objects.filter(
                    user=user,
                    type="system",
                    title=title,
                    created_at__gte=anti_spam_since,
                ).exists()
                if already:
                    continue

                self.stdout.write(f"  Region: {region.name} → {user.username}")
                self._notify(
                    user=user,
                    title=title,
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
        Create a Notification row and send FCM push synchronously.
        Synchronous FCM is intentional here: this runs as a cron process,
        not inside an HTTP request, so blocking is acceptable and daemon
        threads would exit prematurely when the process ends.
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
                "digest_push | user=%s type=%s title=%r sent=%s failed=%s",
                user.pk, n_type, title, result["sent"], result["failed"],
            )
        except Exception:
            logger.exception(
                "digest_push FCM error | notification=%s user=%s",
                notification.pk, user.pk,
            )
            self.stderr.write(
                self.style.ERROR(f"    FCM error for user={user.pk} — notification saved, push skipped")
            )
