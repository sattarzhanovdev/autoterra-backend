import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'autoterra_backend.settings')
django.setup()

from django.contrib.auth.models import User, Group
from api.models import Profile, ClientProfile, Distributor, Region

def seed():
    password = "password123"

    # Setup Groups
    group_client, _ = Group.objects.get_or_create(name='client')
    group_expert, _ = Group.objects.get_or_create(name='expert')

    # 0. Setup Base Data
    dist_owner_user, _ = User.objects.get_or_create(username="+72000000000")
    dist_owner_user.set_password(password)
    dist_owner_user.save()
    Profile.objects.update_or_create(user=dist_owner_user, defaults={'role': 'distributor'})

    base_dist, _ = Distributor.objects.get_or_create(
        user=dist_owner_user,
        name="AutoTerra Central",
        inn="7700000001",
        phone="+72000000000",
        email="central@autoterra.ru"
    )

    region, _ = Region.objects.get_or_create(
        code="77",
        name="Москва и МО",
        distributor=base_dist
    )

    roles = {
        'admin': ('+7000', 'Admin'),
        'manager': ('+7100', 'Manager'),
        'distributor': ('+7200', 'Distributor'),
        'client': ('+7300', 'Client'),
        'courier': ('+7400', 'Courier'),
        'expert': ('+7500', 'Expert'),
    }

    for role, (prefix, label) in roles.items():
        print(f"Creating 5 {label} users...")
        for i in range(1, 6):
            username = f"{prefix}{i:07d}"
            user, created = User.objects.get_or_create(username=username)
            user.set_password(password)
            user.save()

            # Update Profile
            profile, _ = Profile.objects.update_or_create(user=user, defaults={'role': role})

            # Assign Groups
            if role == 'client':
                user.groups.add(group_client)

                ClientProfile.objects.update_or_create(
                    user=user,
                    defaults={
                        'inn': f"12345{prefix[2:]}{i:02d}",
                        'company_name': f"СТО {label} #{i}",
                        'region': region,
                        'distributor': base_dist,
                        'city': "Москва",
                        'contact_name': f"{label} User {i}",
                        'phone': username
                    }
                )
            elif role == 'expert':
                user.groups.add(group_expert)

            elif role == 'distributor':
                Distributor.objects.update_or_create(
                    user=user,
                    defaults={
                        'name': f"Дистрибьютор {i}",
                        'inn': f"99000{i:05d}",
                        'phone': username,
                        'email': f"dist{i}@example.com"
                    }
                )

    print("\nSeeding complete!")

if __name__ == "__main__":
    seed()
