from datetime import datetime, timezone
from decimal import Decimal
from django.conf import settings
from django.db import migrations


def snapshot(apps, schema_editor):
    db = schema_editor.connection.alias
    Rules = apps.get_model('api', 'ReferralRuleVersion')
    Referral = apps.get_model('api', 'Referral')
    Client = apps.get_model('api', 'ClientProfile')
    Purchase = apps.get_model('api', 'Purchase')
    Order = apps.get_model('api', 'Order')
    Item = apps.get_model('api', 'OrderItem')
    Event = apps.get_model('api', 'ReferralAccrual')
    raw = getattr(settings, 'REFERRAL_BONUS_TIERS', None) or [(0, '.5'), (100000, '1'), (200000, '1.5'), (400000, '2'), (800000, '2.5')]
    rule = Rules.objects.using(db).create(effective_from=datetime(1970, 1, 1, tzinfo=timezone.utc), tiers=[[str(a), str(b)] for a, b in raw], activity_min=Decimal(str(getattr(settings, 'REFERRAL_ACTIVITY_MIN', 10000))))
    for referral in Referral.objects.using(db).all().iterator():
        client = Client.objects.using(db).filter(inn=referral.invitee_inn).first()
        if client is None or referral.confirmation not in ('auto', 'confirmed'):
            continue
        operations = []
        for purchase in Purchase.objects.using(db).filter(client=client, status='verified'):
            Purchase.objects.using(db).filter(pk=purchase.pk).update(verified_at=purchase.created_at)
            operations.append((purchase.created_at, 'purchase', purchase.pk, purchase.total_amount))
        for order in Order.objects.using(db).filter(client=client, status='fulfilled'):
            amount = sum((i.price * i.quantity for i in Item.objects.using(db).filter(order=order)), Decimal(0))
            operations.append((order.confirmed_at or order.created_at, 'order', order.pk, amount))
        turnover = Decimal(0)
        for confirmed, kind, pk, amount in sorted(operations):
            turnover += amount
            # Existing ledger is immutable: historical sources are marked as
            # processed, without creating credits or reallocating old credits.
            Event.objects.using(db).create(referral=referral, version=rule, confirmed_at=confirmed, turnover=turnover, amount=0, legacy_snapshot=True, **{kind+'_id': pk})


class Migration(migrations.Migration):
    dependencies = [('api', '0067_referralruleversion_purchase_verified_at_and_more')]
    operations = [migrations.RunPython(snapshot, migrations.RunPython.noop)]
