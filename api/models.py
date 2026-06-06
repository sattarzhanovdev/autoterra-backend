from django.contrib.auth.models import User
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator


class Profile(models.Model):
    class Role(models.TextChoices):
        CLIENT = "client", "Клиент (автосервис)"
        DISTRIBUTOR = "distributor", "Дистрибьютор"
        MANAGER = "manager", "Менеджер импортера"
        ADMIN = "admin", "Центральный админ"
        COURIER = "courier", "Курьер"
        AI_EXPERT = "ai_expert", "Эксперт базы знаний"

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField("Роль", max_length=20, choices=Role.choices, default=Role.CLIENT)

    class Meta:
        verbose_name = "Профиль пользователя"
        verbose_name_plural = "Профили пользователей"

    def __str__(self):
        return f"{self.user.username} ({self.get_role_display()})"


class AuthToken(models.Model):
    key = models.CharField(max_length=64, unique=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="auth_tokens")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} · {self.created_at:%d.%m.%Y}"


class Distributor(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.SET_NULL,
        related_name="distributor_profile",
        verbose_name="Аккаунт дистрибьютора",
        blank=True,
        null=True,
    )
    name = models.CharField("Название", max_length=255)
    inn = models.CharField("ИНН", max_length=12, unique=True)
    external_id = models.CharField("Внешний ID (1C)", max_length=128, blank=True, null=True, db_index=True)
    regions = models.JSONField("Регионы", default=list, blank=True)
    phone = models.CharField("Телефон", max_length=32)
    email = models.EmailField("Email")
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        verbose_name = "Дистрибьютор"
        verbose_name_plural = "Дистрибьюторы"
        ordering = ("name",)

    def __str__(self):
        return self.name


class Region(models.Model):
    code = models.CharField("Код", max_length=32, unique=True)
    name = models.CharField("Название", max_length=128, unique=True)
    distributor = models.ForeignKey(
        Distributor,
        on_delete=models.SET_NULL,
        related_name="managed_regions",
        verbose_name="Дистрибьютор",
        null=True,
        blank=True,
    )
    manager = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="managed_regions",
        verbose_name="Менеджер",
        blank=True,
        null=True,
    )
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        verbose_name = "Регион"
        verbose_name_plural = "Регионы"
        ordering = ("name",)

    def __str__(self):
        return self.name


class ClientProfile(models.Model):
    inn_validator = RegexValidator(
        regex=r"^\d{10}(\d{2})?$",
        message="ИНН должен состоять из 10 или 12 цифр.",
    )

    CATEGORY_CHOICES = [("a", "A"), ("b", "B"), ("c", "C")]
    STATUS_CHOICES = [
        ("new", "Новый"),
        ("under_review", "На проверке"),
        ("active", "Активный"),
        ("blocked", "Заблокирован"),
    ]
    SOURCE_CHOICES = [
        ("app", "Мобильное приложение"),
        ("web", "Веб-сайт"),
        ("manager", "Менеджер"),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="client_profile")
    inn = models.CharField("ИНН", max_length=12, validators=[inn_validator])
    external_id = models.CharField("Внешний ID (1C)", max_length=128, blank=True, null=True, db_index=True)
    company_name = models.CharField("Компания", max_length=255)
    category = models.CharField("Категория", max_length=1, choices=CATEGORY_CHOICES, default="b")
    region = models.ForeignKey(
        Region,
        on_delete=models.PROTECT,
        related_name="clients",
        verbose_name="Регион",
    )
    city = models.CharField("Город", max_length=128)
    contact_name = models.CharField("Контакт", max_length=255)
    phone = models.CharField("Телефон", max_length=32)
    distributor = models.ForeignKey(
        Distributor,
        on_delete=models.PROTECT,
        related_name="clients",
        verbose_name="Дистрибьютор",
        null=True,
        blank=True,
    )
    manager = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="managed_clients",
        verbose_name="Менеджер",
        blank=True,
        null=True,
    )
    registration_source = models.CharField(
        "Источник регистрации",
        max_length=32,
        choices=SOURCE_CHOICES,
        default="app",
    )
    status = models.CharField("Статус", max_length=32, choices=STATUS_CHOICES, default="new")
    partner_status = models.CharField("Партнёрский статус", max_length=32, default="Silver")
    total_purchases = models.DecimalField("Сумма закупок", max_digits=12, decimal_places=2, default=0)
    comments = models.TextField("Комментарии", blank=True)
    created_at = models.DateTimeField("Создан", auto_now_add=True)

    class Meta:
        verbose_name = "Клиент"
        verbose_name_plural = "Клиенты"
        ordering = ("company_name",)
        constraints = [
            models.UniqueConstraint(fields=("inn", "region"), name="unique_client_inn_region"),
        ]

    def __str__(self):
        return self.company_name

    def clean(self):
        # INN validation logic
        if self.inn and self.region:
            # Check for existing INN in the SAME region
            existing_same = ClientProfile.objects.filter(inn=self.inn, region=self.region).exclude(pk=self.pk)
            if existing_same.exists():
                raise ValidationError(f"Клиент с ИНН {self.inn} уже существует в регионе {self.region.name}.")

    def save(self, *args, **kwargs):
        # Trigger clean for validation
        self.full_clean()

        # 1. Auto-assign distributor from region
        if not self.distributor and self.region and self.region.distributor:
            self.distributor = self.region.distributor
        
        # 2. Status logic: if INN exists in ANOTHER region, set to under_review
        if self.inn and self.region:
            existing_other = ClientProfile.objects.filter(inn=self.inn).exclude(region=self.region).exclude(pk=self.pk)
            if existing_other.exists():
                self.status = "under_review"

        super().save(*args, **kwargs)


class Store(models.Model):
    client = models.ForeignKey(ClientProfile, on_delete=models.CASCADE, related_name="stores", verbose_name="Клиент")
    name = models.CharField("Название", max_length=255)
    address = models.CharField("Адрес", max_length=255)
    is_active = models.BooleanField("Активен", default=True)
    created_at = models.DateTimeField("Создан", auto_now_add=True)

    class Meta:
        verbose_name = "Магазин / точка"
        verbose_name_plural = "Магазины / точки"
        ordering = ("name",)

    def __str__(self):
        return self.name


class Product(models.Model):
    STOCK_CHOICES = [
        ("inStock", "В наличии"),
        ("low", "Мало"),
        ("onOrder", "Под заказ"),
        ("outOfStock", "Нет в наличии"),
    ]

    distributor = models.ForeignKey(
        Distributor,
        on_delete=models.CASCADE,
        related_name="products",
        verbose_name="Дистрибьютор",
    )
    sku = models.CharField("Артикул", max_length=64)
    external_id = models.CharField("Внешний ID (1C)", max_length=128, blank=True, null=True, db_index=True)
    name = models.CharField("Название", max_length=255)
    category = models.CharField("Категория", max_length=128)
    brand = models.CharField("Бренд", max_length=128, default="AutoTerra")
    volume = models.DecimalField("Объём", max_digits=8, decimal_places=2, default=0)
    price = models.DecimalField("Цена", max_digits=12, decimal_places=2, default=0)
    quantity = models.PositiveIntegerField("Остаток", default=0)
    status = models.CharField("Статус", max_length=32, choices=STOCK_CHOICES, default="inStock")
    is_active = models.BooleanField("Показывать в приложении", default=True)
    updated_at = models.DateTimeField("Обновлён", auto_now=True)

    class Meta:
        verbose_name = "Товар"
        verbose_name_plural = "Ассортимент"
        unique_together = ("distributor", "sku")
        ordering = ("category", "name")

    def __str__(self):
        return f"{self.sku} · {self.name}"


class Order(models.Model):
    STATUS_CHOICES = [
        ("new", "Новый"),
        ("accepted", "Принят"),
        ("rejected", "Отклонён"),
        ("fulfilled", "Выполнен"),
    ]

    client = models.ForeignKey(ClientProfile, on_delete=models.CASCADE, related_name="orders", verbose_name="Клиент")
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="orders", verbose_name="Где забрать")
    distributor = models.ForeignKey(Distributor, on_delete=models.PROTECT, related_name="orders", verbose_name="Дистрибьютор")
    external_id = models.CharField("Внешний ID (1C)", max_length=128, blank=True, null=True, db_index=True)
    comment = models.TextField("Комментарий", blank=True)
    status = models.CharField("Статус", max_length=32, choices=STATUS_CHOICES, default="new")
    rejection_reason = models.TextField("Причина отклонения", blank=True)
    created_at = models.DateTimeField("Создан", auto_now_add=True)

    class Meta:
        verbose_name = "Заказ"
        verbose_name_plural = "Заказы"
        ordering = ("-created_at",)

    def __str__(self):
        return f"Заказ #{self.id} · {self.client}"

    @property
    def total_amount(self):
        return sum(item.total for item in self.items.all())


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items", verbose_name="Заказ")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="order_items", verbose_name="Товар")
    sku = models.CharField("SKU", max_length=64)
    name = models.CharField("Название", max_length=255)
    category = models.CharField("Категория", max_length=128)
    brand = models.CharField("Бренд", max_length=128)
    volume = models.DecimalField("Объём", max_digits=8, decimal_places=2, default=0)
    price = models.DecimalField("Цена", max_digits=12, decimal_places=2, default=0)
    quantity = models.PositiveIntegerField("Количество", default=1)

    class Meta:
        verbose_name = "Позиция заказа"
        verbose_name_plural = "Позиции заказа"

    @property
    def total(self):
        return self.price * self.quantity

    def __str__(self):
        return f"{self.name} x {self.quantity}"


class Purchase(models.Model):
    STATUS_CHOICES = [
        ("new", "Новая"),
        ("pending", "На проверке"),
        ("pending_verification", "Ожидает подтверждения"),
        ("under_review", "Ручная проверка"),
        ("duplicate_review", "Проверка дубля"),
        ("verified", "Подтверждена"),
        ("rejected", "Отклонена"),
    ]

    client = models.ForeignKey(ClientProfile, on_delete=models.CASCADE, related_name="purchases", verbose_name="Клиент")
    distributor = models.ForeignKey(Distributor, on_delete=models.PROTECT, related_name="purchases", verbose_name="Дистрибьютор")
    document_number = models.CharField("Номер документа", max_length=128)
    date = models.DateField("Дата")
    total_amount = models.DecimalField("Сумма", max_digits=12, decimal_places=2, default=0)
    status = models.CharField("Статус", max_length=32, choices=STATUS_CHOICES, default="pending_verification")
    document_url = models.CharField("Файл/ссылка", max_length=255, blank=True)
    document_file = models.FileField("Файл документа", upload_to="purchases/%Y/%m/", blank=True, null=True)
    document_hash = models.CharField("Хэш документа", max_length=64, blank=True, db_index=True)
    rejection_reason = models.TextField("Причина отклонения", blank=True)
    created_at = models.DateTimeField("Создана", auto_now_add=True)

    class Meta:
        verbose_name = "Покупка"
        verbose_name_plural = "Покупки"
        ordering = ("-date",)

    def __str__(self):
        return self.document_number


class PurchaseItem(models.Model):
    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name="items", verbose_name="Покупка")
    sku = models.CharField("SKU", max_length=64)
    name = models.CharField("Название", max_length=255)
    category = models.CharField("Категория", max_length=128)
    quantity = models.PositiveIntegerField("Количество", default=1)
    volume = models.DecimalField("Объём", max_digits=8, decimal_places=2, default=0)
    price = models.DecimalField("Цена", max_digits=12, decimal_places=2, default=0)
    brand = models.CharField("Бренд", max_length=128, default="AutoTerra")

    class Meta:
        verbose_name = "Позиция покупки"
        verbose_name_plural = "Позиции покупки"


class Attachment(models.Model):
    FILE_TYPE_CHOICES = [
        ("image", "Изображение"),
        ("pdf", "PDF"),
        ("video", "Видео"),
        ("document", "Документ"),
    ]

    file = models.FileField("Файл", upload_to="attachments/%Y/%m/")
    file_type = models.CharField("Тип файла", max_length=32, choices=FILE_TYPE_CHOICES)
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="uploaded_attachments",
        verbose_name="Загрузил",
        blank=True,
        null=True,
    )
    uploaded_at = models.DateTimeField("Загружен", auto_now_add=True)
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    related_object = GenericForeignKey("content_type", "object_id")
    description = models.CharField("Описание", max_length=255, blank=True)

    class Meta:
        verbose_name = "Вложение"
        verbose_name_plural = "Вложения"
        ordering = ("-uploaded_at",)
        indexes = [
            models.Index(fields=("content_type", "object_id")),
        ]

    def __str__(self):
        return self.file.name


class ColorRequest(models.Model):
    STATUS_CHOICES = [
        ("created", "Создана"),
        ("inProgress", "В работе"),
        ("ready", "Готова"),
        ("delivered", "Выдана"),
    ]
    TRANSFER_CHOICES = [
        ("courier", "Курьер"),
        ("self_delivery", "Сам привезу"),
    ]

    client = models.ForeignKey(ClientProfile, on_delete=models.CASCADE, related_name="color_requests", verbose_name="Клиент")
    car_brand = models.CharField("Марка", max_length=128)
    car_model = models.CharField("Модель", max_length=128)
    car_year = models.CharField("Год", max_length=4, blank=True)
    vin = models.CharField("VIN", max_length=32)
    color_code = models.CharField("Код цвета", max_length=64)
    color_name = models.CharField("Название цвета", max_length=128, blank=True)
    urgent = models.BooleanField("Срочно", default=False)
    comment = models.TextField("Комментарий", blank=True)
    
    transfer_method = models.CharField("Способ передачи", max_length=32, choices=TRANSFER_CHOICES, default="courier")
    pickup_address = models.CharField("Адрес забора лючка", max_length=255, blank=True)
    pickup_time = models.DateTimeField("Дата/время забора", blank=True, null=True)
    contact_person = models.CharField("Контактное лицо", max_length=255, blank=True)
    contact_phone = models.CharField("Телефон", max_length=32, blank=True)
    
    sla_deadline = models.DateTimeField("SLA deadline", blank=True, null=True)
    assigned_distributor = models.ForeignKey(
        Distributor,
        on_delete=models.SET_NULL,
        related_name="assigned_color_requests",
        verbose_name="Назначенный дистрибьютор",
        blank=True,
        null=True,
    )
    assigned_station = models.CharField("Назначенная станция", max_length=255, blank=True)
    
    status = models.CharField("Статус", max_length=32, choices=STATUS_CHOICES, default="created")
    status_history = models.JSONField("История статусов", default=list, blank=True)
    recipe = models.TextField("Рецепт", blank=True)
    created_at = models.DateTimeField("Создана", auto_now_add=True)

    class Meta:
        verbose_name = "Заявка на колеровку"
        verbose_name_plural = "Колеровка"
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.car_brand} {self.car_model} · {self.color_code}"

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        if is_new and not self.sla_deadline:
            from django.utils import timezone
            sla_hours = 4 if self.urgent else 24
            self.sla_deadline = timezone.now() + timezone.timedelta(hours=sla_hours)
        super().save(*args, **kwargs)


class RecipeMaterial(models.Model):
    color_request = models.ForeignKey(
        ColorRequest,
        on_delete=models.CASCADE,
        related_name="materials",
        verbose_name="Заявка",
    )
    sku = models.CharField("SKU/Material", max_length=64)
    quantity = models.DecimalField("Количество", max_digits=10, decimal_places=3)
    unit = models.CharField("Единица измерения", max_length=16, default="g")
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    version = models.PositiveIntegerField("Версия рецепта", default=1)

    class Meta:
        verbose_name = "Материал рецепта"
        verbose_name_plural = "Материалы рецепта"
        ordering = ("version", "id")

    def __str__(self):
        return f"{self.sku} · {self.quantity} {self.unit}"


class CourierTask(models.Model):
    TYPE_CHOICES = [
        ("delivery", "Доставка"),
        ("pickup", "Забор лючка"),
        ("return", "Возврат лючка"),
        ("color_lab_pickup", "Забор для Color Lab"),
    ]
    STATUS_CHOICES = [
        ("assigned", "Назначен"),
        ("in_progress", "В пути"),
        ("delivered", "Доставлено"),
        ("returned", "Возвращено"),
        ("cancelled", "Отменено"),
    ]

    courier = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="assigned_tasks",
        verbose_name="Курьер",
        null=True,
        blank=True,
    )
    client = models.ForeignKey(
        ClientProfile,
        on_delete=models.CASCADE,
        related_name="courier_tasks",
        verbose_name="Клиент",
    )
    order = models.ForeignKey(
        Order,
        on_delete=models.SET_NULL,
        related_name="courier_tasks",
        verbose_name="Связанный заказ",
        blank=True,
        null=True,
    )
    color_request = models.ForeignKey(
        ColorRequest,
        on_delete=models.SET_NULL,
        related_name="courier_tasks",
        verbose_name="Заявка Color Lab",
        blank=True,
        null=True,
    )
    task_type = models.CharField(
        "Тип задачи", max_length=32, choices=TYPE_CHOICES, default="delivery"
    )
    address = models.CharField("Адрес", max_length=255)
    time_slot = models.CharField("Временной интервал", max_length=64)
    status = models.CharField(
        "Статус", max_length=32, choices=STATUS_CHOICES, default="assigned"
    )
    status_history = models.JSONField("История статусов", default=list, blank=True)
    car_description = models.CharField("Автомобиль", max_length=255, blank=True)
    contact_name = models.CharField("Контактное лицо", max_length=255, blank=True)
    contact_phone = models.CharField("Телефон", max_length=32, blank=True)
    scheduled_time = models.DateTimeField("Запланировано", blank=True, null=True)
    comment = models.TextField("Комментарий", blank=True)
    courier_comment = models.TextField("Комментарий курьера", blank=True)
    proof_photo = models.ImageField("Фото-отчет", upload_to="courier_proofs/%Y/%m/", blank=True, null=True)
    created_at = models.DateTimeField("Создана", auto_now_add=True)

    class Meta:
        verbose_name = "Задача курьера"
        verbose_name_plural = "Задачи курьера"
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.get_task_type_display()} · {self.address}"


# Signals for CourierTask
from django.db.models.signals import post_save
from django.dispatch import receiver

@receiver(post_save, sender=ColorRequest)
def create_color_lab_courier_task(sender, instance, created, **kwargs):
    if created and instance.transfer_method == 'courier':
        CourierTask.objects.create(
            client=instance.client,
            color_request=instance,
            task_type="color_lab_pickup",
            address=instance.pickup_address or instance.client.city,
            time_slot=instance.pickup_time.strftime("%H:%M") if instance.pickup_time else "В течение дня",
            scheduled_time=instance.pickup_time,
            car_description=f"{instance.car_brand} {instance.car_model}",
            contact_name=instance.contact_person or instance.client.contact_name,
            contact_phone=instance.contact_phone or instance.client.phone,
            comment=instance.comment or "Забор лючка для Color Lab"
        )


class AuditLog(models.Model):
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Пользователь")
    action = models.CharField("Действие", max_length=255)
    model_name = models.CharField("Модель", max_length=100, blank=True)
    object_id = models.CharField("ID объекта", max_length=100, blank=True)
    changes = models.JSONField("Изменения", default=dict, blank=True)
    created_at = models.DateTimeField("Дата/время", auto_now_add=True)

    class Meta:
        verbose_name = "Лог аудита"
        verbose_name_plural = "Логи аудита"
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.created_at}: {self.action}"


class Referral(models.Model):
    inviter = models.ForeignKey(ClientProfile, on_delete=models.CASCADE, related_name="referrals", verbose_name="Кто пригласил")
    invitee_inn = models.CharField("ИНН приглашённого", max_length=12)
    invitee_name = models.CharField("Название приглашённого", max_length=255)
    region = models.CharField("Регион", max_length=128)
    is_registered = models.BooleanField("Зарегистрирован", default=False)
    has_purchase = models.BooleanField("Есть покупка", default=False)
    purchase_amount = models.DecimalField("Сумма покупки", max_digits=12, decimal_places=2, default=0)
    condition_met = models.BooleanField("Условие выполнено", default=False)
    gift = models.CharField("Подарок", max_length=255, blank=True)
    created_at = models.DateTimeField("Создан", auto_now_add=True)

    class Meta:
        verbose_name = "Реферал"
        verbose_name_plural = "Рефералы"
        ordering = ("-created_at",)

    def sync_from_invitee(self):
        invitee = ClientProfile.objects.filter(inn=self.invitee_inn).first()
        if invitee is None:
            return self

        # Anti-fraud: only count verified purchases and fulfilled orders
        purchase_total = (
            invitee.purchases.filter(status="verified").aggregate(total=models.Sum("total_amount"))["total"]
            or 0
        )
        
        # Order.total_amount is a property, sum it manually from the queryset
        orders = invitee.orders.filter(status="fulfilled").prefetch_related("items")
        order_total = sum((o.total_amount for o in orders), start=0)
        
        amount = float(purchase_total) + float(order_total)

        updates = []
        if not self.is_registered:
            self.is_registered = True
            updates.append("is_registered")
            
        if amount > 0 and not self.has_purchase:
            self.has_purchase = True
            updates.append("has_purchase")
            
        if float(self.purchase_amount) != amount:
            self.purchase_amount = amount
            updates.append("purchase_amount")
            
        # Threshold for bonus/gift (e.g. 30,000)
        BONUS_THRESHOLD = 30000
        if amount >= BONUS_THRESHOLD and not self.condition_met:
            self.condition_met = True
            self.gift = "Сертификат на 5000 ₽"
            updates.append("condition_met")
            updates.append("gift")

        if updates:
            self.save(update_fields=updates)
        return self


class ExpertTicket(models.Model):
    STATUS_CHOICES = [
        ("open", "Открыт"),
        ("aiAnswered", "Ответ AI"),
        ("escalated", "Передан эксперту"),
        ("expertAnswered", "Ответ эксперта"),
        ("closed", "Закрыт"),
    ]
    RISK_CHOICES = [
        ("low", "Низкий"),
        ("medium", "Средний"),
        ("high", "Высокий"),
    ]

    client = models.ForeignKey(ClientProfile, on_delete=models.CASCADE, related_name="expert_tickets", verbose_name="Клиент")
    question = models.TextField("Вопрос")
    category = models.CharField("Категория", max_length=128)
    risk = models.CharField("Риск", max_length=16, choices=RISK_CHOICES, default="low")
    status = models.CharField("Статус", max_length=32, choices=STATUS_CHOICES, default="open")
    
    ai_draft_answer = models.TextField("AI черновик ответа", blank=True)
    ai_answer = models.TextField("Ответ AI (опубликованный)", blank=True)
    expert_answer = models.TextField("Ответ эксперта", blank=True)
    
    photo = models.ImageField("Фото дефекта", upload_to="tickets/photos/%Y/%m/", blank=True, null=True)
    video_link = models.URLField("Ссылка на видео", blank=True)
    
    linked_knowledge_card = models.ForeignKey(
        "KnowledgeCard",
        on_delete=models.SET_NULL,
        related_name="source_tickets_legacy",
        verbose_name="Связанная база знаний",
        blank=True,
        null=True,
    )
    
    similar_cases = models.JSONField("Похожие кейсы", default=list, blank=True)
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлён", auto_now=True)

    class Meta:
        verbose_name = "Вопрос эксперту"
        verbose_name_plural = "Вопросы эксперту"
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.category}: {self.question[:50]}..."


class Notification(models.Model):
    TYPE_CHOICES = [
        ("order", "Заказ"),
        ("color", "Колеровка"),
        ("delivery", "Доставка"),
        ("referral", "Реферал"),
        ("ai", "AI"),
        ("system", "Система"),
    ]

    client = models.ForeignKey(ClientProfile, on_delete=models.CASCADE, related_name="notifications", verbose_name="Клиент")
    title = models.CharField("Заголовок", max_length=255)
    body = models.TextField("Текст")
    type = models.CharField("Тип", max_length=32, choices=TYPE_CHOICES, default="system")
    is_read = models.BooleanField("Прочитано", default=False)
    created_at = models.DateTimeField("Создано", auto_now_add=True)

    class Meta:
        verbose_name = "Уведомление"
        verbose_name_plural = "Уведомления"
        ordering = ("-created_at",)

from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        Profile.objects.get_or_create(user=instance)


@receiver(post_save, sender=User)
def save_user_profile(sender, instance, **kwargs):
    if hasattr(instance, "profile"):
        instance.profile.save()


class KnowledgeCard(models.Model):
    STATUS_CHOICES = [
        ("draft", "Черновик"),
        ("approved", "Одобрено"),
        ("rejected", "Отклонено"),
        ("archived", "Архив"),
    ]

    title = models.CharField("Заголовок", max_length=255, blank=True)
    category = models.CharField("Категория", max_length=128, blank=True)
    problem = models.CharField("Проблема", max_length=255)
    causes = models.TextField("Причины", blank=True)
    solution = models.TextField("Решение")
    skus = models.JSONField("SKU", default=list, blank=True)
    restrictions = models.TextField("Ограничения", blank=True)
    
    status = models.CharField("Статус", max_length=32, choices=STATUS_CHOICES, default="draft")
    
    expert_author = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="authored_knowledge_cards",
        verbose_name="Автор-эксперт",
        null=True,
        blank=True,
    )
    source_ticket = models.ForeignKey(
        ExpertTicket,
        on_delete=models.SET_NULL,
        related_name="derived_knowledge_cards",
        verbose_name="Источник (тикет)",
        null=True,
        blank=True,
    )
    
    revision_history = models.JSONField("История правок", default=list, blank=True)
    created_at = models.DateTimeField("Создана", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлена", auto_now=True)

    class Meta:
        verbose_name = "База знаний"
        verbose_name_plural = "База знаний"
        ordering = ("-created_at",)

    def __str__(self):
        return self.problem or self.title
