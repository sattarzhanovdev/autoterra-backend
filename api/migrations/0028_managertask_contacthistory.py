from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0027_userdevicetoken'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ManagerTask',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('text', models.TextField(verbose_name='Задача')),
                ('deadline', models.DateField(blank=True, null=True, verbose_name='Дедлайн')),
                ('status', models.CharField(
                    choices=[('pending', 'Ожидает'), ('completed', 'Выполнено')],
                    default='pending', max_length=16, verbose_name='Статус'
                )),
                ('comment', models.TextField(blank=True, verbose_name='Комментарий')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создана')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлена')),
                ('client', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='manager_tasks',
                    to='api.clientprofile',
                    verbose_name='Клиент',
                )),
                ('manager', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='manager_tasks',
                    to=settings.AUTH_USER_MODEL,
                    verbose_name='Менеджер',
                )),
            ],
            options={
                'verbose_name': 'Задача менеджера',
                'verbose_name_plural': 'Задачи менеджера',
                'ordering': ('deadline', '-created_at'),
            },
        ),
        migrations.CreateModel(
            name='ContactHistory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('contact_type', models.CharField(
                    choices=[('call', 'Звонок'), ('visit', 'Визит'), ('email', 'Email'), ('other', 'Другое')],
                    default='call', max_length=16, verbose_name='Тип'
                )),
                ('result', models.TextField(verbose_name='Результат')),
                ('date', models.DateTimeField(verbose_name='Дата контакта')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создана')),
                ('client', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='contact_history',
                    to='api.clientprofile',
                    verbose_name='Клиент',
                )),
                ('manager', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='contact_history_entries',
                    to=settings.AUTH_USER_MODEL,
                    verbose_name='Менеджер',
                )),
            ],
            options={
                'verbose_name': 'История контакта',
                'verbose_name_plural': 'История контактов',
                'ordering': ('-date',),
            },
        ),
    ]
