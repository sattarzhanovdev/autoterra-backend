from django.test import TestCase
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from api.models import Region, ClientProfile, Distributor, Profile


class ModelRoleTests(TestCase):
    def setUp(self):
        self.manager_user = User.objects.create_user(username="manager_user", password="password")
        self.distributor_user = User.objects.create_user(username="dist_user", password="password")
        
        self.distributor = Distributor.objects.create(
            user=self.distributor_user,
            name="Test Distributor",
            inn="7701000001",
            phone="+79991112233",
            email="dist@example.com"
        )
        
        self.region_moscow = Region.objects.create(
            code="77",
            name="Москва",
            distributor=self.distributor,
            manager=self.manager_user
        )
        
        self.region_spb = Region.objects.create(
            code="78",
            name="Санкт-Петербург",
            distributor=None # No distributor for this region yet
        )

    def test_profile_creation_on_user_save(self):
        """Test 1: Roles and Profile auto-creation"""
        user = User.objects.create_user(username="new_user", password="password")
        self.assertTrue(Profile.objects.filter(user=user).exists())
        self.assertEqual(user.profile.role, Profile.Role.CLIENT)

    def test_client_profile_creation_and_distributor_assignment(self):
        """Test 1 & 5: Region creation, ClientProfile binding, and auto-distributor assignment"""
        user = User.objects.create_user(username="client1", password="password")
        client = ClientProfile.objects.create(
            user=user,
            inn="1234567890",
            company_name="Test Company",
            region=self.region_moscow,
            city="Москва",
            contact_name="Test Contact",
            phone="+70001112233"
        )
        self.assertEqual(client.region, self.region_moscow)
        # Auto-assigned from region.distributor
        self.assertEqual(client.distributor, self.distributor)

    def test_inn_validation_format(self):
        """Test 2: INN validation (length and digits)"""
        user = User.objects.create_user(username="client_bad_inn", password="password")
        
        # Wrong length (9 digits)
        client = ClientProfile(
            user=user,
            inn="123456789",
            region=self.region_moscow,
            company_name="Test",
            city="Test",
            contact_name="Test",
            phone="+70000000000"
        )
        with self.assertRaises(ValidationError):
            client.full_clean()
            
        # Wrong characters (letters)
        client.inn = "123456789A"
        with self.assertRaises(ValidationError):
            client.full_clean()

        # Correct length (10 digits)
        client.inn = "1234567890"
        client.full_clean() # Should not raise

        # Correct length (12 digits)
        client.inn = "123456789012"
        client.full_clean() # Should not raise

    def test_inn_uniqueness_in_region(self):
        """Test 3: Unique INN in same region"""
        user1 = User.objects.create_user(username="u1", password="p")
        ClientProfile.objects.create(
            user=user1,
            inn="1234567890",
            company_name="C1",
            region=self.region_moscow,
            city="City",
            contact_name="Contact",
            phone="+70000000001"
        )
        
        user2 = User.objects.create_user(username="u2", password="p")
        client2 = ClientProfile(
            user=user2,
            inn="1234567890",
            company_name="C2",
            region=self.region_moscow,
            city="City",
            contact_name="Contact",
            phone="+70000000002"
        )
        
        with self.assertRaises(ValidationError):
            client2.full_clean()

    def test_same_inn_different_regions(self):
        """Test 4: Same INN in DIFFERENT regions (status 'under_review')"""
        user1 = User.objects.create_user(username="u1", password="p")
        ClientProfile.objects.create(
            user=user1,
            inn="1234567890",
            company_name="C1",
            region=self.region_moscow,
            city="City",
            contact_name="Contact",
            phone="+70000000001"
        )
        
        user2 = User.objects.create_user(username="u2", password="p")
        client2 = ClientProfile.objects.create(
            user=user2,
            inn="1234567890",
            company_name="C2",
            region=self.region_spb,
            city="City",
            contact_name="Contact",
            phone="+70000000002"
        )
        
        self.assertEqual(client2.status, "under_review")
        self.assertEqual(ClientProfile.objects.filter(inn="1234567890").count(), 2)
