from django.db import migrations


def archive_existing(apps, schema_editor):
    # Current training content is demonstration content. Keep rows and APIs;
    # future materials can be published normally through the admin.
    apps.get_model('api', 'LearningMaterial').objects.using(schema_editor.connection.alias).filter(status='published').update(status='archived')


class Migration(migrations.Migration):
    dependencies = [('api', '0065_order_bonus_requested_androidrelease')]
    operations = [migrations.RunPython(archive_existing, migrations.RunPython.noop)]
