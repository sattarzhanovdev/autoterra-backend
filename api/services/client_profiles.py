"""Account access rules shared by app APIs and Django administration."""
from django.contrib.auth.models import User


def synchronize_client_access(client):
    if client.status not in ('active', 'archived'):
        return
    active = client.status == 'active'
    User.objects.filter(pk=client.user_id).update(is_active=active)
    client.user.is_active = active
    if not active:
        client.user.auth_tokens.all().delete()
