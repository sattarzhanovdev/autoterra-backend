from datetime import datetime, time
from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator


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
    specialty = models.CharField("Специализация", max_length=255, blank=True)
    bio = models.TextField("О себе", blank=True)
    rating = models.DecimalField("Рейтинг", max_digits=3, decimal_places=1, default=5.0)

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


class IntegrationToken(models.Model):
    distributor = models.ForeignKey(
        Distributor,
        on_delete=models.CASCADE,
        related_name="integration_tokens",
        verbose_name="Дистрибьютор",
    )
    token = models.CharField("Токен", max_length=128, unique=True)
    is_active = models.BooleanField("Активен", default=True)
    created_at = models.DateTimeField("Создан", auto_now_add=True)

    class Meta:
        verbose_name = "Токен интеграции"
        verbose_name_plural = "Токены интеграции"

    def __str__(self):
        return f"{self.distributor.name} · {self.created_at:%d.%m.%Y}"


class SyncLog(models.Model):
    TYPE_CHOICES = [
        ("stock_update", "Обновление остатков"),
        ("orders_export", "Экспорт заказов"),
    ]
    STATUS_CHOICES = [
        ("success", "Успех"),
        ("error", "Ошибка"),
    ]

    distributor = models.ForeignKey(
        Distributor,
        on_delete=models.CASCADE,
        related_name="sync_logs",
        verbose_name="Дистрибьютор",
    )
    sync_type = models.CharField("Тип синхронизации", max_length=32, choices=TYPE_CHOICES)
    status = models.CharField("Статус", max_length=16, choices=STATUS_CHOICES)
    details = models.JSONField("Детали", default=dict, blank=True)
    created_at = models.DateTimeField("Дата/время", auto_now_add=True)

    class Meta:
        verbose_name = "Лог синхронизации"
        verbose_name_plural = "Логи синхронизации"
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.created_at:%d.%m.%Y %H:%M} · {self.get_sync_type_display()} · {self.get_status_display()}"


class PartnerTier(models.Model):
    """Ранг клиента и порог оборота, с которого он начинается.

    Ранги настраиваются в админке: и названия, и пороги. Клиент растёт по
    обороту — сумме подтверждённых закупок и оплаченных заказов, — а от ранга
    зависит цена (модель RankDiscount).

    Ступени сравниваются по ``threshold``: у кого порог выше, тот старше.
    """

    name = models.CharField("Название", max_length=32, unique=True)
    threshold = models.DecimalField(
        "Порог оборота, ₽",
        max_digits=14,
        decimal_places=2,
        default=0,
        help_text="Начиная с этой суммы клиент получает ранг. У базового ранга — 0.",
    )
    description = models.CharField("Описание", max_length=255, blank=True)
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        verbose_name = "Ранг клиента"
        verbose_name_plural = "Ранги клиентов"
        ordering = ("threshold",)

    def __str__(self):
        return self.name


# Алфавит без похожих символов: 0/O, 1/I/l — код диктуют по телефону и
# переписывают с экрана, путаница тут стоит потерянного реферала.
REFERRAL_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
REFERRAL_CODE_PREFIX = "AT-"
REFERRAL_CODE_LENGTH = 6


def generate_referral_code():
    """Личный код приглашения. Уникальность проверяется по базе."""
    import secrets

    for _ in range(20):
        body = "".join(secrets.choice(REFERRAL_CODE_ALPHABET) for _ in range(REFERRAL_CODE_LENGTH))
        code = f"{REFERRAL_CODE_PREFIX}{body}"
        if not ClientProfile.objects.filter(referral_code=code).exists():
            return code
    # Практически недостижимо: 31^6 ≈ 887 млн вариантов.
    raise RuntimeError("Не удалось подобрать свободный реферальный код")


# Пороги на случай пустой таблицы: первый запуск, тесты, миграция с нуля.
DEFAULT_PARTNER_TIERS = [
    ("Базовый", 0, "Стартовый ранг при регистрации"),
    ("Silver", 500_000, "Оборот от 500 тыс. ₽"),
    ("Gold", 2_000_000, "Оборот от 2 млн ₽"),
    ("Platinum", 5_000_000, "Оборот от 5 млн ₽"),
]

BASE_PARTNER_TIER = DEFAULT_PARTNER_TIERS[0][0]

# Статусы заказа, при которых деньги уже получены. Такой заказ идёт в оборот
# клиента и создаёт задачу курьеру. 'accepted' — legacy-статус старых заказов.
ORDER_STATUSES_PAID = ("paid", "shipped", "fulfilled", "accepted")


def partner_tier_ladder():
    """Активные ранги от младшего к старшему. Пустая таблица — дефолтные."""
    tiers = list(PartnerTier.objects.filter(is_active=True).order_by("threshold"))
    if tiers:
        return [(tier.name, Decimal(tier.threshold)) for tier in tiers]
    return [(name, Decimal(threshold)) for name, threshold, _ in DEFAULT_PARTNER_TIERS]


def partner_tier_for_total(total):
    """Заслуженный ранг по обороту."""
    ladder = partner_tier_ladder()
    amount = Decimal(str(total or 0))
    earned = ladder[0][0]
    for name, threshold in ladder:
        if amount >= threshold:
            earned = name
    return earned


def grown_partner_status(current, total):
    """Повышает ранг до заслуженного, но не понижает.

    Понижение — отдельное решение менеджера: сезонный провал не должен молча
    обвалить клиенту цену. Массовый пересчёт с понижением умеет команда
    ``recalc_partner_tiers --allow-downgrade``.
    """
    ladder = partner_tier_ladder()
    order = {name: index for index, (name, _) in enumerate(ladder)}

    earned = partner_tier_for_total(total)
    if current not in order:
        return earned
    return earned if order[earned] > order[current] else current


def client_turnover(client):
    """Оборот клиента: подтверждённые закупки плюс оплаченные заказы.

    Закупка — документ, который клиент загрузил и дистрибьютор подтвердил.
    Заказ через приложение — те же деньги, поэтому тоже растит ранг: иначе
    активный в приложении клиент навсегда оставался бы в базовом ранге.
    """
    purchases = (
        Purchase.objects
        .filter(client=client, status="verified")
        .aggregate(total=models.Sum("total_amount"))["total"]
        or 0
    )
    # Order.total_amount — вычисляемое свойство, поэтому сумму собираем по
    # позициям прямо в SQL: перебирать заказы в Python было бы N+1.
    orders = (
        OrderItem.objects
        .filter(order__client=client, order__status__in=ORDER_STATUSES_PAID)
        .aggregate(
            total=models.Sum(
                models.F("price") * models.F("quantity"),
                output_field=models.DecimalField(max_digits=14, decimal_places=2),
            )
        )["total"]
        or 0
    )
    return Decimal(str(purchases)) + Decimal(str(orders))


class ClientProfile(models.Model):
    inn_validator = RegexValidator(
        regex=r"^\d{10}(\d{2})?$",
        message="ИНН должен состоять из 10 или 12 цифр.",
    )

    # Тип бизнеса клиента. На цену не влияет — цену определяет ранг
    # (partner_status), см. PartnerTier и RankDiscount.
    CATEGORY_CHOICES = [
        ("a", "A · Дилерский салон"),
        ("b", "B · Автосервис с кузовным цехом"),
        ("c", "C · Гаражный сервис"),
    ]
    STATUS_CHOICES = [
        ("new", "Новый"),
        ("under_review", "На проверке"),
        ("active", "Активный"),
        ("blocked", "Заблокирован"),
        # Архив — не то же самое, что блокировка: клиент не нарушал правил,
        # он просто перестал работать. Блокировкой такое помечать нельзя.
        ("archived", "Архивный"),
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
    # Ранг клиента: определяет его цену. Растёт по обороту, пороги задаются
    # в админке (PartnerTier), скидки — в RankDiscount.
    partner_status = models.CharField(
        "Ранг",
        max_length=32,
        default=BASE_PARTNER_TIER,
        help_text="Присваивается автоматически по обороту. Менеджер может выставить вручную.",
    )
    # Личный код для приглашений. По нему новый сервис при регистрации
    # связывается с пригласившим — см. Referral и эндпоинт /register.
    referral_code = models.CharField(
        "Реферальный код",
        max_length=16,
        unique=True,
        blank=True,
        null=True,
        db_index=True,
    )
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
        # Личный код нужен до валидации: full_clean проверит уникальность.
        if not self.referral_code:
            self.referral_code = generate_referral_code()

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


# Максимум фотографий на один товар. Сами файлы не храним — только ссылки
# (в шаблоне WB они приходят одной ячейкой через ';').
MAX_PRODUCT_IMAGES = 15

# Разумный предел длины ссылки — совпадает с max_length поля video_url.
MAX_IMAGE_URL_LENGTH = 512


def normalize_product_images(value):
    """Приводит значение к списку ссылок на фото: не более MAX_PRODUCT_IMAGES.

    Принимает список/кортеж или строку со ссылками через ';' (',' и перенос
    строки тоже считаем разделителями — так пишут в Excel). Пустые значения,
    дубликаты и всё, что не http(s)-ссылка, отбрасываются; порядок ссылок
    сохраняется, лишние сверх лимита отсекаются.
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        raw = value.replace("\n", ";").replace(",", ";").split(";")
    elif isinstance(value, (list, tuple)):
        raw = value
    else:
        return []

    urls = []
    for item in raw:
        url = str(item or "").strip()
        if not url or url in urls:
            continue
        if not url.lower().startswith(("http://", "https://")):
            continue
        urls.append(url[:MAX_IMAGE_URL_LENGTH])
    return urls[:MAX_PRODUCT_IMAGES]


def validate_product_images(value):
    """Валидатор поля Product.images (срабатывает в формах и full_clean)."""
    if not isinstance(value, list):
        raise ValidationError("Фото: ожидается список ссылок.")
    if len(value) > MAX_PRODUCT_IMAGES:
        raise ValidationError(
            f"Фото: не более {MAX_PRODUCT_IMAGES} шт. на товар (передано {len(value)})."
        )
    for url in value:
        if not isinstance(url, str) or not url.strip():
            raise ValidationError("Фото: пустая ссылка в списке.")
        if len(url) > MAX_IMAGE_URL_LENGTH:
            raise ValidationError(
                f"Фото: ссылка длиннее {MAX_IMAGE_URL_LENGTH} символов — {url[:60]}…"
            )
        if not url.lower().startswith(("http://", "https://")):
            raise ValidationError(f"Фото: ссылка должна начинаться с http:// или https:// — {url[:60]}")


class Product(models.Model):
    MAX_IMAGES = MAX_PRODUCT_IMAGES

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
    sku = models.CharField("Артикул продавца", max_length=64)
    external_id = models.CharField("Внешний ID (1C)", max_length=128, blank=True, null=True, db_index=True)
    wb_article = models.CharField("Артикул WB", max_length=64, blank=True, db_index=True)
    group_name = models.CharField("Группа", max_length=128, blank=True)
    name = models.CharField("Название", max_length=255)
    category = models.CharField("Категория", max_length=128)
    brand = models.CharField("Бренд", max_length=128, default="AutoTerra")
    description = models.TextField("Описание", blank=True)
    color = models.CharField("Цвет", max_length=128, blank=True)
    barcode = models.CharField("Баркод", max_length=64, blank=True, db_index=True)
    images = models.JSONField(
        "Фото (ссылки)",
        default=list,
        blank=True,
        validators=[validate_product_images],
        help_text=f"Список http(s)-ссылок на фото, максимум {MAX_PRODUCT_IMAGES} шт. В Excel — одной ячейкой через «;».",
    )
    video_url = models.URLField("Видео", max_length=512, blank=True)
    volume = models.DecimalField("Объём", max_digits=8, decimal_places=2, default=0)
    # Габариты и вес упаковки (из шаблона WB)
    weight = models.DecimalField("Вес с упаковкой (кг)", max_digits=8, decimal_places=3, default=0)
    package_height = models.DecimalField("Высота упаковки (см)", max_digits=8, decimal_places=2, default=0)
    package_length = models.DecimalField("Длина упаковки (см)", max_digits=8, decimal_places=2, default=0)
    package_width = models.DecimalField("Ширина упаковки (см)", max_digits=8, decimal_places=2, default=0)
    tnved = models.CharField("ТНВЭД", max_length=32, blank=True)
    vat_rate = models.CharField("Ставка НДС", max_length=16, blank=True)
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

    def save(self, *args, **kwargs):
        # Нормализуем фото на любом пути записи: импорт Excel, API, админка,
        # синхронизация с 1С. В БД всегда лежит чистый список ссылок ≤ 15.
        self.images = normalize_product_images(self.images)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.sku} · {self.name}"


class RankDiscount(models.Model):
    """Скидка на категорию товаров для ранга клиента.

    Ранг растёт по обороту: чем больше клиент закупает, тем ниже его цена.
    Правило задаётся в админке и применяется к прайсу автоматически.

    Правила ищутся от частного к общему, побеждает первое совпавшее:

        1. этот дистрибьютор + эта категория товаров
        2. этот дистрибьютор + все категории
        3. все дистрибьюторы + эта категория товаров
        4. все дистрибьюторы + все категории

    Пустой «Дистрибьютор» или «Категория товаров» = «любой/любая».
    """

    distributor = models.ForeignKey(
        "Distributor",
        on_delete=models.CASCADE,
        related_name="rank_discounts",
        verbose_name="Дистрибьютор",
        blank=True,
        null=True,
        help_text="Пусто — правило действует у всех дистрибьюторов",
    )
    tier = models.ForeignKey(
        PartnerTier,
        on_delete=models.CASCADE,
        related_name="discounts",
        verbose_name="Ранг клиента",
    )
    product_category = models.CharField(
        "Категория товаров",
        max_length=128,
        blank=True,
        help_text="Пусто — скидка на весь ассортимент",
    )
    percent = models.DecimalField(
        "Скидка, %",
        max_digits=5,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    is_active = models.BooleanField("Активно", default=True)
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        verbose_name = "Скидка по рангу"
        verbose_name_plural = "Скидки по рангам"
        unique_together = ("distributor", "tier", "product_category")
        ordering = ("tier__threshold", "product_category")

    def __str__(self):
        scope = self.product_category or "весь ассортимент"
        return f"{self.tier} · {scope} · −{self.percent}%"


class Order(models.Model):
    STATUS_CHOICES = [
        ("new", "Новый"),                # клиент оформил, ждёт проверки оператором
        ("confirmed", "Подтверждён"),    # оператор подтвердил наличие, готов к оплате
        ("adjusted", "Скорректирован"),  # оператор изменил состав, ждёт согласия клиента
        ("accepted", "Принят"),          # legacy-статус (сохранён для обратной совместимости)
        ("rejected", "Отклонён"),        # оператор отклонил заказ
        ("paid", "Оплачен"),             # клиент оплатил подтверждённый заказ
        ("shipped", "Отправлен"),        # товар отправлен клиенту
        ("fulfilled", "Выполнен"),       # товар доставлен, заказ завершён
        ("cancelled", "Отменён"),        # клиент отменил заказ (только до оплаты)
    ]

    # Разрешённые переходы статусов (state machine). Ключ — текущий статус,
    # значение — множество допустимых следующих статусов. Служит единственным
    # источником правды для валидации во всех endpoint-ах заказа.
    STATUS_TRANSITIONS = {
        "new": {"confirmed", "adjusted", "rejected", "cancelled", "accepted"},
        "confirmed": {"paid", "cancelled"},
        "adjusted": {"confirmed", "paid", "cancelled"},  # confirmed = клиент согласился
        "accepted": {"paid", "shipped", "rejected", "fulfilled", "cancelled"},  # legacy
        "rejected": set(),
        "paid": {"shipped"},
        "shipped": {"fulfilled"},
        "fulfilled": set(),
        "cancelled": set(),
    }

    # Статусы, в которых клиент может инициировать оплату
    PAYABLE_STATUSES = {"confirmed", "adjusted"}

    DELIVERY_CHOICES = [
        ("courier", "Курьерская доставка"),
        ("self_pickup", "Самовывоз"),
    ]

    client = models.ForeignKey(ClientProfile, on_delete=models.CASCADE, related_name="orders", verbose_name="Клиент")
    store = models.ForeignKey(Store, on_delete=models.SET_NULL, related_name="orders", verbose_name="Где забрать", null=True, blank=True)
    distributor = models.ForeignKey(Distributor, on_delete=models.PROTECT, related_name="orders", verbose_name="Дистрибьютор")
    delivery_method = models.CharField("Способ получения", max_length=32, choices=DELIVERY_CHOICES, default="courier")
    external_id = models.CharField("Внешний ID (1C)", max_length=128, blank=True, null=True, db_index=True)
    comment = models.TextField("Комментарий", blank=True)
    status = models.CharField("Статус", max_length=32, choices=STATUS_CHOICES, default="new")
    rejection_reason = models.TextField("Причина отклонения", blank=True)
    
    courier = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="assigned_orders",
        verbose_name="Курьер",
        null=True,
        blank=True,
    )
    estimated_delivery_date = models.DateField("Ожидаемая дата доставки", null=True, blank=True)

    confirmed_at = models.DateTimeField("Подтверждён", null=True, blank=True)
    paid_at = models.DateTimeField("Оплачен", null=True, blank=True)
    shipped_at = models.DateTimeField("Отправлен", null=True, blank=True)

    created_at = models.DateTimeField("Создан", auto_now_add=True)

    class Meta:
        verbose_name = "Заказ"
        verbose_name_plural = "Заказы"
        ordering = ("-created_at",)

    def can_transition_to(self, new_status):
        """True if new_status is a valid next state from the current status."""
        if new_status == self.status:
            return False
        return new_status in self.STATUS_TRANSITIONS.get(self.status, set())

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


class OrderAdjustment(models.Model):
    """История корректировок заказа оператором (для прозрачности перед клиентом)."""

    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="adjustments", verbose_name="Заказ"
    )
    original_items = models.JSONField("Изначальные позиции", default=list)
    adjusted_items = models.JSONField("Подтверждённые позиции", default=list)
    reason = models.TextField("Комментарий оператора", blank=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="order_adjustments",
        verbose_name="Кто скорректировал",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField("Создано", auto_now_add=True)

    class Meta:
        verbose_name = "Корректировка заказа"
        verbose_name_plural = "Корректировки заказов"
        ordering = ("-created_at",)

    def __str__(self):
        return f"Корректировка заказа #{self.order_id}"


class Payment(models.Model):
    """Платёж по заказу через YooKassa (ЮKassa / YooMoney для бизнеса)."""

    STATUS_CHOICES = [
        ("pending", "Ожидает оплаты"),
        ("waiting_for_capture", "Ожидает подтверждения"),
        ("succeeded", "Оплачен"),
        ("canceled", "Отменён"),
    ]

    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="payments", verbose_name="Заказ"
    )
    provider = models.CharField("Провайдер", max_length=32, default="yookassa")
    provider_payment_id = models.CharField(
        "ID платежа у провайдера", max_length=128, blank=True, db_index=True
    )
    amount = models.DecimalField("Сумма", max_digits=12, decimal_places=2, default=0)
    currency = models.CharField("Валюта", max_length=8, default="RUB")
    status = models.CharField("Статус", max_length=32, choices=STATUS_CHOICES, default="pending")
    confirmation_url = models.URLField("Ссылка на оплату", max_length=512, blank=True)
    idempotence_key = models.CharField("Ключ идемпотентности", max_length=64, blank=True)
    raw_response = models.JSONField("Ответ провайдера", default=dict, blank=True)
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    paid_at = models.DateTimeField("Оплачен", null=True, blank=True)

    class Meta:
        verbose_name = "Платёж"
        verbose_name_plural = "Платежи"
        ordering = ("-created_at",)

    def __str__(self):
        return f"Платёж {self.provider_payment_id or self.pk} · {self.get_status_display()}"


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
    # Тип покрытия задаёт состав рецепта и цену: по нему клиент добирает краску
    # в тот же цвет, и добор считается по цене своего типа.
    PAINT_TYPE_CHOICES = [
        ("acrylic", "Акрил"),
        ("baseClear", "База + лак"),
        ("threeStage", "Трёхстадийная"),
    ]

    client = models.ForeignKey(ClientProfile, on_delete=models.CASCADE, related_name="color_requests", verbose_name="Клиент")
    car_brand = models.CharField("Марка", max_length=128)
    # Модель, год и VIN больше не спрашиваются при подборе: цвет определяют
    # марка и код, а лишние поля только удлиняли форму. В базе остаются —
    # в старых заявках они заполнены, и терять эти данные нельзя.
    car_model = models.CharField("Модель", max_length=128, blank=True)
    car_year = models.CharField("Год", max_length=4, blank=True)
    vin = models.CharField("VIN", max_length=32, blank=True)
    color_code = models.CharField("Код цвета", max_length=64)
    color_name = models.CharField("Название цвета", max_length=128, blank=True)
    paint_type = models.CharField(
        "Тип покрытия", max_length=32, choices=PAINT_TYPE_CHOICES, default="baseClear"
    )
    # Уточнение к типу покрытия: маляр дописывает своими словами, если из трёх
    # вариантов ни один не описывает состав точно.
    paint_type_note = models.CharField("Уточнение по покрытию", max_length=255, blank=True)
    urgent = models.BooleanField("Срочно", default=False)
    comment = models.TextField("Комментарий", blank=True)
    
    transfer_method = models.CharField("Способ передачи", max_length=32, choices=TRANSFER_CHOICES, default="courier")
    pickup_address = models.CharField("Адрес забора лючка", max_length=255, blank=True)
    pickup_time = models.DateTimeField("Дата/время забора", blank=True, null=True)
    # Крайнее время, до которого маляр может принять курьера: колорист и курьер
    # планируют выезд по этому дедлайну.
    courier_arrive_until = models.TimeField("Курьер может приехать до", blank=True, null=True)
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
        return f"{self.car_brand} {self.car_model} · {self.color_code} · {self.get_paint_type_display()}"

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
    # «Возврат лючка» (return) убран: готовую краску и образец маляр забирает
    # сам, чтобы сверить оттенок на месте. Старые записи чистит команда
    # cleanup_return_tasks.
    TYPE_CHOICES = [
        ("delivery", "Доставка"),
        ("pickup", "Забор лючка"),
        ("color_lab_pickup", "Забор для Color Lab"),
    ]
    STATUS_CHOICES = [
        ("created", "Создан"),
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
        "Статус", max_length=32, choices=STATUS_CHOICES, default="created"
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

def _task_history_entry(status, comment=""):
    """Отметка в истории заявки. Формат совпадает с _append_task_history в views."""
    from django.utils import timezone

    return {"status": status, "at": timezone.now().isoformat(), "by": None, "comment": comment}


# Доставка создаётся ровно тогда, когда деньги получены.
ORDER_STATUSES_WITH_DELIVERY = ORDER_STATUSES_PAID


@receiver(post_save, sender=Order)
def manage_order_courier_task(sender, instance, created, **kwargs):
    """Создаёт задачу курьеру для оплаченного заказа с курьерской доставкой.

    Раньше триггером был только 'accepted' — legacy-статус: заказы, идущие
    современным путём (new → confirmed → paid → shipped), доставку не получали
    вовсе, и у клиента раздел «Доставка» оставался пустым.

    Адрес берём у точки, которую клиент выбрал в заказе: у профиля клиента
    улицы нет, только город.
    """
    if instance.delivery_method != "courier":
        return
    if instance.status not in ORDER_STATUSES_WITH_DELIVERY:
        return

    address = instance.store.address if instance.store_id else instance.client.city
    scheduled = None
    if instance.estimated_delivery_date:
        from django.utils import timezone

        scheduled = timezone.make_aware(
            datetime.combine(instance.estimated_delivery_date, time(10, 0)),
            timezone.get_current_timezone(),
        )

    history = [_task_history_entry("created", f"Заказ ORD-{instance.id:05d} оплачен")]
    if instance.courier_id:
        history.append(_task_history_entry("assigned", "Курьер назначен на заказ"))

    task, created_task = CourierTask.objects.get_or_create(
        order=instance,
        defaults={
            "client": instance.client,
            "task_type": "delivery",
            "address": address,
            "time_slot": "10:00 - 18:00",
            "scheduled_time": scheduled,
            "status": "assigned" if instance.courier_id else "created",
            "courier": instance.courier,
            "comment": f"Доставка заказа ORD-{instance.id:05d}",
            "contact_name": instance.client.contact_name,
            "contact_phone": instance.client.phone,
            "status_history": history,
        },
    )
    if not created_task and task.courier_id != instance.courier_id:
        # Курьера назначили (или сменили) уже после создания задачи.
        task.courier = instance.courier
        if instance.courier_id and task.status == "created":
            task.status = "assigned"
            task.status_history = [
                *(task.status_history or []),
                _task_history_entry("assigned", "Курьер назначен на заказ"),
            ]
        task.save(update_fields=["courier", "status", "status_history"])


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
    # Номинал подарка. Ноль — подарок не деньгами (статус, отсрочка): такой на
    # бонусный счёт не попадает, его отрабатывает дистрибьютор вне приложения.
    gift_amount = models.DecimalField(
        "Номинал подарка", max_digits=12, decimal_places=2, default=0
    )

    # Подарок может быть скидкой или отсрочкой, а это деньги дистрибьютора —
    # по п. 7 ТЗ выдавать его без согласования нельзя. Поэтому выполненное
    # условие и выданный подарок — разные состояния.
    GIFT_STATUS_CHOICES = [
        ("none", "Условие не выполнено"),
        ("pending", "Ждёт согласования"),
        ("approved", "Согласован"),
        ("declined", "Отклонён"),
    ]
    gift_status = models.CharField(
        "Согласование подарка", max_length=16, choices=GIFT_STATUS_CHOICES, default="none"
    )
    gift_decided_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="referral_gift_decisions",
        verbose_name="Кто решил",
        blank=True,
        null=True,
    )
    gift_decided_at = models.DateTimeField("Когда решено", blank=True, null=True)
    gift_comment = models.CharField("Комментарий к решению", max_length=255, blank=True)
    created_at = models.DateTimeField("Создан", auto_now_add=True)

    # Кто кого привёл — со слов пригласившего. Ручную заявку по чужому ИНН может
    # подать кто угодно, поэтому она не даёт права на подарок, пока сам
    # приглашённый её не подтвердит. Регистрация по коду подтверждения не
    # требует: код и есть доказательство.
    CONFIRMATION_CHOICES = [
        ("auto", "По реферальному коду"),
        ("pending", "Ждёт подтверждения приглашённого"),
        ("confirmed", "Приглашённый подтвердил"),
        ("declined", "Приглашённый отказался"),
    ]
    confirmation = models.CharField(
        "Подтверждение приглашённым",
        max_length=16,
        choices=CONFIRMATION_CHOICES,
        default="auto",
    )
    confirmed_at = models.DateTimeField("Когда подтверждено", blank=True, null=True)

    class Meta:
        verbose_name = "Реферал"
        verbose_name_plural = "Рефералы"
        ordering = ("-created_at",)
        constraints = [
            # Один пригласивший не может заявить одно СТО дважды. Глобальную
            # уникальность по ИНН держит create_referral: там она сопровождается
            # понятным ответом, а не 500-й от базы.
            models.UniqueConstraint(
                fields=("inviter", "invitee_inn"),
                name="referral_unique_inviter_invitee",
            ),
        ]

    @property
    def gift_is_issued(self) -> bool:
        """Подарок можно показывать клиенту как полученный."""
        return self.condition_met and self.gift_status == "approved"

    @property
    def counts_toward_bonus(self) -> bool:
        """Засчитывается ли связка при начислении подарка.

        Неподтверждённая заявка не считается: иначе достаточно было бы вписать
        ИНН чужого клиента и получить подарок за его покупки.
        """
        return self.confirmation in ("auto", "confirmed")

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
            
        # Бонус считается процентом от оборота приглашённого и капает на
        # бонусный счёт автоматически — начислением занимается
        # api.services.referral_bonus. Здесь только отмечаем, что условие
        # выполнено: с первого же рубля ставка уже не нулевая.
        #
        # Плоский подарок «набрал 30 000 — получи сертификат на 5 000» убран:
        # это было 16,7% от оборота, выше маржи. Поля gift* остались ради
        # истории уже выданных подарков.
        if amount > 0 and not self.condition_met and self.counts_toward_bonus:
            self.condition_met = True
            updates.append("condition_met")
            # Notification + FCM push are sent via api.signals._referral_post_save

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
    expert_author = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="answered_tickets",
        verbose_name="Ответивший эксперт",
        null=True,
        blank=True,
    )
    
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
        ("info", "Информация"),
        ("action_required", "Требуется действие"),
        ("recommendation", "Рекомендация"),
        ("order", "Заказ"),
        ("color", "Колеровка"),
        ("delivery", "Доставка"),
        ("referral", "Реферал"),
        ("ai", "AI"),
        ("system", "Система"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications", verbose_name="Пользователь", null=True)
    client = models.ForeignKey(ClientProfile, on_delete=models.CASCADE, related_name="notifications_legacy", verbose_name="Клиент", null=True, blank=True)
    title = models.CharField("Заголовок", max_length=255)
    body = models.TextField("Текст")
    type = models.CharField("Тип", max_length=32, choices=TYPE_CHOICES, default="info")
    related_link = models.CharField("Ссылка", max_length=255, blank=True)
    is_read = models.BooleanField("Прочитано", default=False)
    created_at = models.DateTimeField("Создано", auto_now_add=True)

    class Meta:
        verbose_name = "Уведомление"
        verbose_name_plural = "Уведомления"
        ordering = ("-created_at",)


class UserDeviceToken(models.Model):
    PLATFORM_CHOICES = [
        ("android", "Android"),
        ("ios", "iOS"),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="device_tokens",
        verbose_name="Пользователь",
    )
    token = models.TextField("FCM-токен", unique=True)
    platform = models.CharField(
        "Платформа", max_length=10, choices=PLATFORM_CHOICES, default="android"
    )
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        verbose_name = "FCM-токен устройства"
        verbose_name_plural = "FCM-токены устройств"

    def __str__(self):
        return f"{self.user_id} / {self.platform} / {self.token[:20]}..."


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


class BonusTransaction(models.Model):
    """Движение по бонусному счёту клиента.

    Баланс намеренно не хранится отдельным полем, а считается суммой операций:
    это деньги, и по каждому рублю должно быть видно, откуда он взялся и куда
    ушёл. Начисление — плюс, списание в счёт заказа — минус.
    """

    KIND_CHOICES = [
        ("referral", "Начислен по реферальной программе"),
        ("order", "Списан в счёт заказа"),
        ("refund", "Возвращён после отмены оплаты"),
        ("expired", "Сгорел из-за неактивности"),
        ("manual", "Ручная корректировка"),
    ]

    client = models.ForeignKey(
        ClientProfile,
        on_delete=models.CASCADE,
        related_name="bonus_transactions",
        verbose_name="Клиент",
    )
    amount = models.DecimalField("Сумма", max_digits=12, decimal_places=2)
    kind = models.CharField("Тип", max_length=16, choices=KIND_CHOICES)
    referral = models.ForeignKey(
        "Referral",
        on_delete=models.SET_NULL,
        related_name="bonus_transactions",
        verbose_name="Реферал",
        blank=True,
        null=True,
    )
    order = models.ForeignKey(
        Order,
        on_delete=models.SET_NULL,
        related_name="bonus_transactions",
        verbose_name="Заказ",
        blank=True,
        null=True,
    )
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    created_at = models.DateTimeField("Создана", auto_now_add=True)

    class Meta:
        verbose_name = "Операция по бонусам"
        verbose_name_plural = "Операции по бонусам"
        ordering = ("-created_at",)
        # Ограничения «один реферал — одно начисление» больше нет: бонус
        # капает процентом по мере закупок приглашённого, и операций по одной
        # связке столько же, сколько у него оплат. От задвоения защищает не
        # база, а расчёт разницы в api.services.referral_bonus.accrue: он
        # начисляет только то, чего не хватает до текущей ступени.

    def __str__(self):
        return f"{self.client_id}: {self.amount} ({self.get_kind_display()})"


class LearningMaterial(models.Model):
    """Обучающий материал: урок, чек-лист, видео, инструкция, вебинар (п. 10 ТЗ).

    От KnowledgeCard отличается назначением: карточка — это разбор конкретной
    проблемы для ответов AI, а материал клиент изучает целиком. Поэтому у него
    есть ссылка на видео и файл, которых у карточки нет.

    Управляется из админки: клиент материалы только читает.
    """

    KIND_CHOICES = [
        ("lesson", "Урок"),
        ("checklist", "Чек-лист"),
        ("video", "Видео"),
        ("manual", "Инструкция"),
        ("webinar", "Запись вебинара"),
    ]
    STATUS_CHOICES = [
        ("draft", "Черновик"),
        ("published", "Опубликован"),
        ("archived", "Архив"),
    ]

    title = models.CharField("Заголовок", max_length=255)
    kind = models.CharField("Тип", max_length=16, choices=KIND_CHOICES, default="lesson")
    category = models.CharField("Категория", max_length=128, blank=True)
    summary = models.CharField("Краткое описание", max_length=512, blank=True)
    body = models.TextField("Содержание", blank=True)
    video_url = models.URLField("Ссылка на видео", max_length=512, blank=True)
    file_url = models.URLField("Ссылка на файл", max_length=512, blank=True)
    duration_minutes = models.PositiveIntegerField("Длительность, мин", blank=True, null=True)
    status = models.CharField("Статус", max_length=16, choices=STATUS_CHOICES, default="draft")
    author = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="learning_materials",
        verbose_name="Автор",
        blank=True,
        null=True,
    )
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлён", auto_now=True)

    class Meta:
        verbose_name = "Обучающий материал"
        verbose_name_plural = "Обучающие материалы"
        ordering = ("-created_at",)

    def __str__(self):
        return self.title


class ManagerTask(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Ожидает'),
        ('completed', 'Выполнено'),
    ]
    client = models.ForeignKey(
        ClientProfile,
        on_delete=models.SET_NULL,
        related_name='manager_tasks',
        verbose_name='Клиент',
        null=True,
        blank=True,
    )
    manager = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='manager_tasks',
        verbose_name='Менеджер',
    )
    text = models.TextField('Задача')
    deadline = models.DateField('Дедлайн', null=True, blank=True)
    status = models.CharField('Статус', max_length=16, choices=STATUS_CHOICES, default='pending')
    comment = models.TextField('Комментарий', blank=True)
    created_at = models.DateTimeField('Создана', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлена', auto_now=True)

    class Meta:
        verbose_name = 'Задача менеджера'
        verbose_name_plural = 'Задачи менеджера'
        ordering = ('deadline', '-created_at')

    def __str__(self):
        return f"{self.client.company_name}: {self.text[:50]}"


class ContactHistory(models.Model):
    CONTACT_TYPES = [
        ('call', 'Звонок'),
        ('visit', 'Визит'),
        ('email', 'Email'),
        ('other', 'Другое'),
    ]
    client = models.ForeignKey(
        ClientProfile,
        on_delete=models.CASCADE,
        related_name='contact_history',
        verbose_name='Клиент',
    )
    manager = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='contact_history_entries',
        verbose_name='Менеджер',
    )
    contact_type = models.CharField('Тип', max_length=16, choices=CONTACT_TYPES, default='call')
    result = models.TextField('Результат')
    date = models.DateTimeField('Дата контакта')
    created_at = models.DateTimeField('Создана', auto_now_add=True)

    class Meta:
        verbose_name = 'История контакта'
        verbose_name_plural = 'История контактов'
        ordering = ('-date',)

    def __str__(self):
        return f"{self.client.company_name} · {self.get_contact_type_display()} · {self.date:%d.%m.%Y}"


@receiver(post_save, sender=ClientProfile)
def sync_client_distributor_data(sender, instance, **kwargs):
    """
    If a client's distributor is updated, migrate all historical and pending
    orders, purchases, and color requests to the new distributor.
    """
    if instance.distributor:
        # Sync orders
        instance.orders.all().update(distributor=instance.distributor)
        # Sync purchases
        instance.purchases.all().update(distributor=instance.distributor)
        # Sync color requests
        instance.color_requests.all().update(assigned_distributor=instance.distributor)

