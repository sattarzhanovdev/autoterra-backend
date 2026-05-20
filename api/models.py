from django.contrib.auth.models import User
from django.db import models


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


class ClientProfile(models.Model):
    CATEGORY_CHOICES = [("a", "A"), ("b", "B"), ("c", "C")]
    STATUS_CHOICES = [
        ("newClient", "Новый"),
        ("pending", "На проверке"),
        ("active", "Активный"),
        ("blocked", "Заблокирован"),
        ("archived", "Архив"),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="client_profile")
    inn = models.CharField("ИНН", max_length=12, unique=True)
    company_name = models.CharField("Компания", max_length=255)
    category = models.CharField("Категория", max_length=1, choices=CATEGORY_CHOICES, default="b")
    region = models.CharField("Регион", max_length=128)
    city = models.CharField("Город", max_length=128)
    contact_name = models.CharField("Контакт", max_length=255)
    phone = models.CharField("Телефон", max_length=32)
    distributor = models.ForeignKey(
        Distributor,
        on_delete=models.PROTECT,
        related_name="clients",
        verbose_name="Дистрибьютор",
    )
    status = models.CharField("Статус", max_length=32, choices=STATUS_CHOICES, default="active")
    partner_status = models.CharField("Партнёрский статус", max_length=32, default="Silver")
    total_purchases = models.DecimalField("Сумма закупок", max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField("Создан", auto_now_add=True)

    class Meta:
        verbose_name = "Клиент"
        verbose_name_plural = "Клиенты"
        ordering = ("company_name",)

    def __str__(self):
        return self.company_name


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
        ("pending", "Новый"),
        ("accepted", "Принят"),
        ("rejected", "Отклонён"),
        ("done", "Выполнен"),
    ]

    client = models.ForeignKey(ClientProfile, on_delete=models.CASCADE, related_name="orders", verbose_name="Клиент")
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="orders", verbose_name="Где забрать")
    distributor = models.ForeignKey(Distributor, on_delete=models.PROTECT, related_name="orders", verbose_name="Дистрибьютор")
    comment = models.TextField("Комментарий", blank=True)
    status = models.CharField("Статус", max_length=32, choices=STATUS_CHOICES, default="pending")
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
    STATUS_CHOICES = [("pending", "На проверке"), ("verified", "Подтверждена"), ("rejected", "Отклонена")]

    client = models.ForeignKey(ClientProfile, on_delete=models.CASCADE, related_name="purchases", verbose_name="Клиент")
    distributor = models.ForeignKey(Distributor, on_delete=models.PROTECT, related_name="purchases", verbose_name="Дистрибьютор")
    document_number = models.CharField("Номер документа", max_length=128)
    date = models.DateField("Дата")
    total_amount = models.DecimalField("Сумма", max_digits=12, decimal_places=2, default=0)
    status = models.CharField("Статус", max_length=32, choices=STATUS_CHOICES, default="pending")
    document_url = models.CharField("Файл/ссылка", max_length=255, blank=True)
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


class ColorRequest(models.Model):
    STATUS_CHOICES = [
        ("created", "Создана"),
        ("inProgress", "В работе"),
        ("ready", "Готова"),
        ("delivered", "Выдана"),
    ]

    client = models.ForeignKey(ClientProfile, on_delete=models.CASCADE, related_name="color_requests", verbose_name="Клиент")
    car_brand = models.CharField("Марка", max_length=128)
    car_model = models.CharField("Модель", max_length=128)
    vin = models.CharField("VIN", max_length=32)
    color_code = models.CharField("Код цвета", max_length=64)
    color_name = models.CharField("Название цвета", max_length=128, blank=True)
    urgent = models.BooleanField("Срочно", default=False)
    courier_pickup = models.BooleanField("Нужен курьер для лючка", default=False)
    status = models.CharField("Статус", max_length=32, choices=STATUS_CHOICES, default="created")
    recipe = models.TextField("Рецепт", blank=True)
    created_at = models.DateTimeField("Создана", auto_now_add=True)

    class Meta:
        verbose_name = "Заявка на колеровку"
        verbose_name_plural = "Колеровка"
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.car_brand} {self.car_model} · {self.color_code}"


class CourierTask(models.Model):
    TYPE_CHOICES = [("delivery", "Доставка"), ("pickup", "Забор лючка"), ("return", "Возврат лючка")]
    STATUS_CHOICES = [
        ("created", "Создана"),
        ("assigned", "Назначен курьер"),
        ("inProgress", "В пути"),
        ("delivered", "Доставлено"),
        ("returned", "Возвращено"),
    ]

    client = models.ForeignKey(ClientProfile, on_delete=models.CASCADE, related_name="courier_tasks", verbose_name="Клиент")
    type = models.CharField("Тип", max_length=32, choices=TYPE_CHOICES, default="delivery")
    address = models.CharField("Адрес", max_length=255)
    scheduled_time = models.DateTimeField("Время")
    contact_name = models.CharField("Контакт", max_length=255)
    contact_phone = models.CharField("Телефон", max_length=32)
    car_description = models.CharField("Авто/описание", max_length=255, blank=True)
    status = models.CharField("Статус", max_length=32, choices=STATUS_CHOICES, default="created")
    courier_id = models.CharField("Курьер", max_length=128, blank=True)
    photo_proof = models.CharField("Фото", max_length=255, blank=True)
    comment = models.TextField("Комментарий", blank=True)
    created_at = models.DateTimeField("Создана", auto_now_add=True)

    class Meta:
        verbose_name = "Доставка"
        verbose_name_plural = "Доставка"
        ordering = ("-scheduled_time",)

    def __str__(self):
        return f"{self.get_type_display()} · {self.address}"


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


class ExpertTicket(models.Model):
    STATUS_CHOICES = [
        ("open", "Открыт"),
        ("aiAnswered", "Ответ AI"),
        ("escalated", "Передан эксперту"),
        ("expertAnswered", "Ответ эксперта"),
        ("closed", "Закрыт"),
    ]

    client = models.ForeignKey(ClientProfile, on_delete=models.CASCADE, related_name="expert_tickets", verbose_name="Клиент")
    question = models.TextField("Вопрос")
    category = models.CharField("Категория", max_length=128)
    ai_answer = models.TextField("Ответ AI", blank=True)
    expert_answer = models.TextField("Ответ эксперта", blank=True)
    status = models.CharField("Статус", max_length=32, choices=STATUS_CHOICES, default="open")
    created_at = models.DateTimeField("Создан", auto_now_add=True)

    class Meta:
        verbose_name = "Вопрос эксперту"
        verbose_name_plural = "Вопросы эксперту"
        ordering = ("-created_at",)


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


class KnowledgeCard(models.Model):
    problem = models.CharField("Проблема", max_length=255)
    causes = models.TextField("Причины")
    solution = models.TextField("Решение")
    skus = models.JSONField("SKU", default=list, blank=True)
    restrictions = models.TextField("Ограничения", blank=True)
    approving_expert = models.CharField("Эксперт", max_length=255)
    is_approved = models.BooleanField("Одобрено", default=True)
    created_at = models.DateTimeField("Создана", auto_now_add=True)

    class Meta:
        verbose_name = "База знаний"
        verbose_name_plural = "База знаний"
        ordering = ("problem",)
