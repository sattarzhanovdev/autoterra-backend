import re
from decimal import Decimal, InvalidOperation
from django import forms
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from django.template.response import TemplateResponse
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.utils.decorators import method_decorator
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from .forms import ProductExcelImportForm
from .order_admin_forms import OrderAdminForm, OrderItemCommandFormSet, OrderMutationReasonForm, OrderStatusCommandForm
from .services import orders as order_service
from .services.exports import EXPORTERS as EXPORT_FORMATS, ExportUnavailable, export_clients
from .models import (
    Attachment,
    AuditLog,
    AuthToken,
    BonusTransaction,
    ClientProfile,
    ClientPriceOverride,
    ClientBrandDiscount,
    ColorRequest,
    ContactHistory,
    CourierTask,
    Distributor,
    ExpertTicket,
    KnowledgeCard,
    LearningMaterial,
    ManagerTask,
    Notification,
    Order,
    OrderAdjustment,
    OrderItem,
    Payment,
    Product,
    Purchase,
    PurchaseItem,
    PartnerTier,
    RankDiscount,
    Region,
    Referral,
    Store,
    Profile,
    MAX_PRODUCT_IMAGES,
)

admin.site.site_header = "AutoTerra Admin"
admin.site.site_title = "AutoTerra"
admin.site.index_title = "Панель управления платформой"
admin.site.index_template = "admin/api/index.html"


PRODUCT_STATUS_ALIASES = {
    "instock": "inStock",
    "in stock": "inStock",
    "в наличии": "inStock",
    "наличие": "inStock",
    "low": "low",
    "мало": "low",
    "заканчивается": "low",
    "onorder": "onOrder",
    "on order": "onOrder",
    "под заказ": "onOrder",
    "outofstock": "outOfStock",
    "out of stock": "outOfStock",
    "нет": "outOfStock",
    "нет в наличии": "outOfStock",
}

PRODUCT_IMPORT_HEADERS = {
    "article": "sku",
    "sku": "sku",
    "артикул": "sku",
    "код": "sku",
    "код товара": "sku",
    "name": "name",
    "product name": "name",
    "item name": "name",
    "product": "name",
    "название": "name",
    "наименование": "name",
    "товар": "name",
    "category": "category",
    "категория": "category",
    "brand": "brand",
    "бренд": "brand",
    "производитель": "brand",
    "volume": "volume",
    "size": "volume",
    "объем": "volume",
    "объём": "volume",
    "фасовка": "volume",
    "price": "price",
    "цена": "price",
    "quantity": "quantity",
    "qty": "quantity",
    "stock": "quantity",
    "balance": "quantity",
    "остаток": "quantity",
    "количество": "quantity",
    "status": "status",
    "статус": "status",
    "наличие": "status",
}

PRODUCT_TEMPLATE_HEADERS = [
    "Артикул",
    "Название",
    "Категория",
    "Бренд",
    "Объём",
    "Цена",
    "Остаток",
    "Статус",
    "Фото",
    "Синонимы",
]

PRODUCT_TEMPLATE_EXAMPLE = [
    "DV-1234",
    "Дверь передняя левая",
    "Кузовные детали",
    "AutoTerra",
    350,
    3200,
    12,
    "В наличии",
    "https://example.com/photo/1.jpg;https://example.com/photo/2.jpg",
    "левая дверь; передняя дверь",
]


def _norm(value):
    return str(value or "").strip()


def _money(value):
    try:
        return Decimal(str(value or "0").replace(",", ".").strip())
    except (InvalidOperation, AttributeError):
        return Decimal("0")


def _int(value):
    try:
        return int(Decimal(str(value or "0").replace(",", ".").strip()))
    except (InvalidOperation, ValueError, AttributeError):
        return 0


def _status(value):
    text = _norm(value).lower()
    return PRODUCT_STATUS_ALIASES.get(text, "inStock")


def _headers(row):
    result = {}
    for index, cell in enumerate(row):
        key = PRODUCT_IMPORT_HEADERS.get(_norm(cell).lower())
        if key:
            result[key] = index
    return result


def _cell(row, headers, key, default=""):
    index = headers.get(key)
    if index is None or index >= len(row):
        return default
    value = row[index]
    return default if value is None else value


class StoreInline(admin.TabularInline):
    model = Store
    extra = 0
    fields = ("name", "address", "is_active", "created_at")
    readonly_fields = ("created_at",)


class ProductInline(admin.TabularInline):
    model = Product
    extra = 0
    fields = ("sku", "name", "category", "brand", "volume", "price", "quantity", "status")



class ClientBrandDiscountInline(admin.TabularInline):
    model = ClientBrandDiscount
    extra = 0
    fields = ("brand", "percent")

    def _allowed(self, request, obj):
        role = getattr(getattr(request.user, "profile", None), "role", None)
        return request.user.is_superuser or role == "admin" or (
            role == "distributor" and obj and obj.distributor and obj.distributor.is_active and obj.distributor.user_id == request.user.pk)

    def has_add_permission(self, request, obj=None):
        return bool(self._allowed(request, obj)) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return bool(self._allowed(request, obj)) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return bool(self._allowed(request, obj)) and super().has_delete_permission(request, obj)


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    fields = ("product", "sku", "name", "quantity", "price", "brand", "category", "volume")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class PurchaseItemInline(admin.TabularInline):
    model = PurchaseItem
    extra = 0


class ProfileInline(admin.StackedInline):
    model = Profile
    can_delete = False
    verbose_name_plural = "Дополнительная информация (Роль)"
    fk_name = "user"
    fields = ("role", "specialty", "rating", "bio")


class UserAdmin(BaseUserAdmin):
    inlines = (ProfileInline,)

    def get_inline_instances(self, request, obj=None):
        if not obj:
            return list()
        return super(UserAdmin, self).get_inline_instances(request, obj)


admin.site.unregister(User)
admin.site.register(User, UserAdmin)


@admin.register(Distributor)
class DistributorAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "inn", "external_id", "user", "phone", "email", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "inn", "external_id", "phone", "email", "user__username", "user__email")
    inlines = (ProductInline,)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        role = getattr(getattr(request.user, "profile", None), "role", None)
        if request.user.is_superuser or role == "admin":
            return qs
        return qs.filter(user=request.user) if role == "distributor" else qs.none()

    def get_readonly_fields(self, request, obj=None):
        role = getattr(getattr(request.user, "profile", None), "role", None)
        if request.user.is_superuser or role == "admin":
            return ()
        return ("inn", "regions", "user", "external_id", "is_active")

    def has_add_permission(self, request):
        role = getattr(getattr(request.user, "profile", None), "role", None)
        return bool(request.user.is_superuser or role == "admin") and super().has_add_permission(request)



@admin.register(Region)
class RegionAdmin(admin.ModelAdmin):
    list_display = ("id", "code", "name", "distributor", "manager", "is_active")
    list_filter = ("is_active", "distributor")
    search_fields = ("code", "name", "distributor__name", "manager__username", "manager__email")

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        role = getattr(getattr(request.user, "profile", None), "role", None)
        if request.user.is_superuser or role == "admin":
            return qs
        if role == "distributor":
            return qs.filter(distributor__user=request.user)
        return qs.filter(manager=request.user) if role == "manager" else qs.none()

    def has_change_permission(self, request, obj=None):
        role = getattr(getattr(request.user, "profile", None), "role", None)
        return bool(request.user.is_superuser or role == "admin") and super().has_change_permission(request, obj)

    def has_add_permission(self, request):
        return self.has_change_permission(request) and super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return self.has_change_permission(request, obj) and super().has_delete_permission(request, obj)



class _CentralPricePermissions:
    """Regional managers edit through the scoped app API, not the global admin."""
    def _can_manage_prices(self, request):
        return request.user.is_superuser or getattr(getattr(request.user, 'profile', None), 'role', None) == 'admin'

    def has_view_permission(self, request, obj=None):
        return self._can_manage_prices(request) and super().has_view_permission(request, obj)

    def has_add_permission(self, request, obj=None):
        return self._can_manage_prices(request) and request.user.has_perm('api.add_clientpriceoverride')

    def has_change_permission(self, request, obj=None):
        return self._can_manage_prices(request) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._can_manage_prices(request) and super().has_delete_permission(request, obj)

    @admin.display(description='Базовая цена')
    def base_price(self, obj):
        return obj.product.price if obj.product_id else '—'


class ClientPriceOverrideInline(_CentralPricePermissions, admin.TabularInline):
    model = ClientPriceOverride
    extra = 0
    autocomplete_fields = ('product',)
    readonly_fields = ('base_price', 'updated_at')


@admin.register(ClientPriceOverride)
class ClientPriceOverrideAdmin(_CentralPricePermissions, admin.ModelAdmin):
    list_display = ('client', 'product', 'base_price', 'price', 'is_active', 'updated_at')
    list_select_related = ('client', 'product')
    search_fields = ('client__company_name', 'client__inn', 'product__name', 'product__sku')
    list_filter = ('is_active', 'client__distributor')
    autocomplete_fields = ('client', 'product')
    readonly_fields = ('base_price', 'updated_at')


@admin.register(ClientProfile)
class ClientProfileAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "company_name",
        "inn",
        "external_id",
        "category",
        "region",
        "city",
        "distributor",
        "manager",
        "status",
        "registration_source",
        "partner_status",
        "referral_count",
        "referral_registered_count",
        "referral_purchase_amount",
    )
    list_filter = ("category", "status", "registration_source", "partner_status", "region", "distributor")
    search_fields = ("company_name", "inn", "external_id", "contact_name", "phone", "user__username", "user__email")
    readonly_fields = ("created_at", "markup_percent")
    inlines = (StoreInline, ClientPriceOverrideInline, ClientBrandDiscountInline)
    actions = ("export_xlsx", "export_docx", "export_pdf", "export_csv")
    change_list_template = "admin/api/clientprofile/change_list.html"

    def get_readonly_fields(self, request, obj=None):
        role = getattr(getattr(request.user, "profile", None), "role", None)
        allowed = request.user.is_superuser or role == "admin" or (
            role == "distributor" and obj is not None and obj.distributor
            and obj.distributor.user_id == request.user.pk
        )
        fields = list(self.readonly_fields)
        if role == "distributor" and not request.user.is_superuser:
            editable = {"personal_discount_percent", "status", "category", "cash_payment_allowed"} if allowed else set()
            return tuple(field.name for field in self.model._meta.fields if field.name not in editable)
        if not allowed:
            fields.extend(["personal_discount_percent", "status", "category"])
        cash_allowed = request.user.is_superuser or role == "admin" or (
            role == "distributor" and obj is not None and obj.distributor and obj.distributor.user_id == request.user.pk
        )
        if not cash_allowed:
            fields.append("cash_payment_allowed")
        return tuple(fields)

    def save_model(self, request, obj, form, change):
        previous = ClientProfile.objects.get(pk=obj.pk) if change else None
        old_cash = previous.cash_payment_allowed if previous else False
        old = previous.personal_discount_percent if previous else None
        super().save_model(request, obj, form, change)
        from .services.client_profiles import synchronize_client_access
        synchronize_client_access(obj)
        if previous:
            for field in ("status", "category"):
                if getattr(previous, field) != getattr(obj, field):
                    AuditLog.objects.create(user=request.user, action=f"Client {field} changed",
                        model_name="ClientProfile", object_id=str(obj.pk), changes={
                            "before": getattr(previous, field), "after": getattr(obj, field)})
        if old_cash != obj.cash_payment_allowed:
            AuditLog.objects.create(user=request.user, action="Client cash permission updated",
                model_name="ClientProfile", object_id=str(obj.pk), changes={
                    "oldCashPaymentAllowed": old_cash, "cashPaymentAllowed": obj.cash_payment_allowed})
        if old != obj.personal_discount_percent:
            AuditLog.objects.create(user=request.user, action="Client discount updated",
                model_name="ClientProfile", object_id=str(obj.pk), changes={
                    "oldPersonalDiscountPercent": str(old) if old is not None else None,
                    "personalDiscountPercent": str(obj.personal_discount_percent) if obj.personal_discount_percent is not None else None,
                })

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related("user", "region", "distributor", "manager")
        role = getattr(getattr(request.user, "profile", None), "role", None)
        if request.user.is_superuser or role == "admin":
            return qs
        if role == "distributor":
            return qs.filter(distributor__user=request.user)
        if role == "manager":
            return qs.filter(region__manager=request.user)
        return qs.none()

    # ── Выгрузка ─────────────────────────────────────────────────────────────

    @admin.action(description="Скачать в Excel (.xlsx)")
    def export_xlsx(self, request, queryset):
        return export_clients(queryset, "xlsx")

    @admin.action(description="Скачать в Word (.docx)")
    def export_docx(self, request, queryset):
        return export_clients(queryset, "docx")

    @admin.action(description="Скачать в PDF")
    def export_pdf(self, request, queryset):
        return export_clients(queryset, "pdf")

    @admin.action(description="Скачать в CSV")
    def export_csv(self, request, queryset):
        return export_clients(queryset, "csv")

    def get_urls(self):
        # Кнопка «Скачать всё» над списком: выгружает текущую выборку целиком,
        # с учётом фильтров и поиска, без ручного выделения галочками.
        custom = [
            path(
                "export/<str:fmt>/",
                self.admin_site.admin_view(self.export_filtered),
                name="api_clientprofile_export",
            ),
        ]
        return custom + super().get_urls()

    def export_filtered(self, request, fmt):
        if fmt not in EXPORT_FORMATS:
            messages.error(request, f"Неизвестный формат: {fmt}")
            return redirect("admin:api_clientprofile_changelist")

        changelist = self.get_changelist_instance(request)
        try:
            return export_clients(changelist.get_queryset(request), fmt)
        except ExportUnavailable as error:
            messages.error(request, str(error))
            return redirect("admin:api_clientprofile_changelist")

    @admin.display(description="Рекомендовал")
    def referral_count(self, obj):
        return obj.referrals.count()

    @admin.display(description="Зарегистр.")
    def referral_registered_count(self, obj):
        referrals = [item.sync_from_invitee() for item in obj.referrals.all()]
        return sum(1 for item in referrals if item.is_registered)

    @admin.display(description="Продажи реф.")
    def referral_purchase_amount(self, obj):
        amount = sum(
            (item.sync_from_invitee().purchase_amount for item in obj.referrals.all()),
            start=Decimal("0"),
        )
        return f"{amount:,.0f}".replace(",", " ")


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "client", "address", "is_active", "created_at")
    list_filter = ("is_active", "client__region", "client__distributor")
    search_fields = ("name", "address", "client__company_name", "client__inn")
    readonly_fields = ("created_at",)


class ProductAdminForm(forms.ModelForm):
    synonyms = forms.CharField(label="Синонимы", required=False,
        widget=forms.Textarea(attrs={"rows": 3}), help_text="Альтернативные названия через «;»")

    class Meta:
        model = Product
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["synonyms"] = "; ".join(self.instance.synonyms or [])

    def clean_synonyms(self):
        from api.services.product_names import normalize_product_synonyms
        return normalize_product_synonyms(self.cleaned_data["synonyms"])


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    form = ProductAdminForm
    change_list_template = "admin/api/product/change_list.html"
    list_display = ("id", "sku", "external_id", "name", "category", "brand", "distributor", "quantity", "status", "price", "photo_preview")
    list_filter = ("status", "category", "brand", "distributor")
    search_fields = ("sku", "external_id", "name", "category", "brand", "distributor__name")
    readonly_fields = ("updated_at", "photo_preview")

    def get_search_results(self, request, queryset, search_term):
        from api.services.search import search_products
        return search_products(queryset, search_term), False

    @admin.display(description="Фото")
    def photo_preview(self, obj):
        """Первое фото + счётчик — быстрый способ проверить импорт ссылок."""
        urls = obj.images or []
        if not urls:
            return "—"
        return format_html(
            '<a href="{}" target="_blank"><img src="{}" style="height:36px;border-radius:4px" '
            'onerror="this.style.display=\'none\'"></a> {}/{}',
            urls[0], urls[0], len(urls), MAX_PRODUCT_IMAGES,
        )

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "import-excel/",
                self.admin_site.admin_view(self.import_excel),
                name="api_product_import_excel",
            ),
            path(
                "template-excel/",
                self.admin_site.admin_view(self.template_excel),
                name="api_product_template_excel",
            ),
        ]
        return custom_urls + urls

    def import_button(self):
        url = reverse("admin:api_product_import_excel")
        return format_html('<a class="button" href="{}">Загрузить Excel</a>', url)

    def template_excel(self, request):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Ассортимент"
        sheet.append(PRODUCT_TEMPLATE_HEADERS)
        sheet.append(PRODUCT_TEMPLATE_EXAMPLE)

        header_fill = PatternFill("solid", fgColor="E31E24")
        header_font = Font(color="FFFFFF", bold=True)
        for cell in sheet[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")

        widths = {
            "A": 18,
            "B": 30,
            "C": 24,
            "D": 22,
            "E": 14,
            "F": 14,
            "G": 14,
            "H": 18,
            "I": 60,  # «Фото» — ссылки через ';', до 15 шт.
            "J": 60,  # «Синонимы»
        }
        for column, width in widths.items():
            sheet.column_dimensions[column].width = width

        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = "A1:J2"
        for row in sheet.iter_rows(min_row=2, max_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical="top")

        status_validation = DataValidation(
            type="list",
            formula1='"В наличии,Мало,Под заказ,Нет в наличии"',
            allow_blank=True,
        )
        sheet.add_data_validation(status_validation)
        status_validation.add("H2:H5000")

        response = HttpResponse(
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        response["Content-Disposition"] = 'attachment; filename="autoterra-products-template.xlsx"'
        workbook.save(response)
        return response

    def import_excel(self, request):
        if request.method == "POST":
            form = ProductExcelImportForm(request.POST, request.FILES)
            if form.is_valid():
                created, updated, errors = self._import_products(
                    distributor=form.cleaned_data["distributor"],
                    file_obj=form.cleaned_data["file"],
                )
                if errors:
                    for error in errors[:10]:
                        messages.warning(request, error)
                    if len(errors) > 10:
                        messages.warning(request, f"И ещё ошибок: {len(errors) - 10}")
                messages.success(
                    request,
                    f"Импорт завершён: создано {created}, обновлено {updated}.",
                )
                return redirect("admin:api_product_changelist")
        else:
            form = ProductExcelImportForm()

        context = {
            **self.admin_site.each_context(request),
            "title": "Загрузка ассортимента из Excel",
            "form": form,
            "opts": self.model._meta,
            "sample_headers_ru": "Артикул продавца, Наименование, Категория продавца, Бренд, Описание, Фото, Баркод, Вес, Габариты, ТНВЭД (+ Цена/Остаток — по желанию)",
            "sample_headers_en": (
                "Поддерживается шаблон Wildberries «Общие характеристики»: колонки определяются "
                "по названию, шапка может быть не в первой строке. В колонке «Фото» — ссылки на "
                f"изображения через «;» (сохраняем до {MAX_PRODUCT_IMAGES} шт. на товар, сами файлы не скачиваем)."
            ),
            "statuses": "Цена и остаток берутся из файла, если такие колонки есть; иначе сохраняются текущие значения.",
        }
        return render(request, "admin/api/product/import_excel.html", context)

    def _import_products(self, distributor, file_obj):
        """Импорт товаров из Excel (шаблон WB «Общие характеристики» и совместимые).

        Разбор вынесен в общий сервис api.services.product_import, чтобы
        одинаково работать и здесь, и в мобильном API.
        """
        from .services.product_import import parse_products_workbook, upsert_products

        products, errors = parse_products_workbook(file_obj)
        if not products:
            return 0, 0, errors
        created, updated = upsert_products(distributor, products)
        return created, updated, errors


class OrderAdjustmentInline(admin.StackedInline):
    model = OrderAdjustment
    extra = 0
    fields = ("created_at", "created_by", "reason", "original_items", "adjusted_items")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    form = OrderAdminForm
    change_form_template = "admin/api/order/change_form.html"
    list_display = ("id", "client", "store", "distributor", "external_id", "status", "created_at")
    list_filter = ("status", "distributor", "store")
    search_fields = ("id", "external_id", "client__company_name", "client__inn", "store__name", "comment", "items__name", "items__sku")
    readonly_fields = ("external_id", "cash_collected_by", "status", "stock_restored", "created_at", "confirmed_at", "paid_at", "shipped_at")
    inlines = (OrderItemInline, OrderAdjustmentInline)
    actions = ("confirm_selected", "reject_selected", "cancel_selected", "ship_selected", "fulfill_selected")
    transition_labels = {
        "confirmed": "Подтвердить заказ", "rejected": "Отклонить заказ",
        "cancelled": "Отменить заказ", "shipped": "Отметить отправку", "fulfilled": "Завершить заказ",
    }

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        role = getattr(getattr(request.user, "profile", None), "role", None)
        if request.user.is_superuser or role == "admin":
            return queryset
        if role == "distributor":
            return queryset.filter(distributor__user=request.user)
        if role == "manager":
            return queryset.filter(client__region__manager=request.user)
        return queryset.none()

    def _client_queryset(self, request, obj):
        queryset = ClientProfile.objects.all()
        role = getattr(getattr(request.user, "profile", None), "role", None)
        if not request.user.is_superuser and role != "admin":
            queryset = queryset.filter(distributor_id=obj.distributor_id)
            if role == "manager":
                queryset = queryset.filter(region__manager=request.user)
        return queryset.filter(Q(status="active") | Q(pk=obj.client_id))

    def has_change_permission(self, request, obj=None):
        if not super().has_change_permission(request, obj):
            return False
        return obj is None or self.get_queryset(request).filter(pk=obj.pk).exists()

    def get_readonly_fields(self, request, obj=None):
        fields = list(self.readonly_fields)
        if not order_service.is_editable_order(obj):
            fields.extend(order_service.METADATA_FIELDS)
        else:
            if obj.status != "new" or obj.payments.exists():
                fields.append("distributor")
            if obj.payments.exists():
                fields.append("payment_method")
        return tuple(dict.fromkeys(fields))

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        editable = order_service.is_editable_order(obj)
        if "adjustment_reason" in form.base_fields:
            form.base_fields["adjustment_reason"].disabled = not editable
        role = getattr(getattr(request.user, "profile", None), "role", None)
        if obj and not request.user.is_superuser and role != "admin":
            if "distributor" in form.base_fields:
                form.base_fields["distributor"].queryset = Distributor.objects.filter(pk=obj.distributor_id)
            if "client" in form.base_fields:
                form.base_fields["client"].queryset = ClientProfile.objects.filter(distributor_id=obj.distributor_id)
        if "client" in form.base_fields and obj:
            form.base_fields["client"].queryset = self._client_queryset(request, obj)
        if "store" in form.base_fields and obj:
            client_id = request.POST.get("client", obj.client_id) if request.method == "POST" else obj.client_id
            try:
                client_id = int(client_id)
            except (ValueError, TypeError):
                client_id = obj.client_id
            form.base_fields["store"].queryset = Store.objects.filter(client_id=client_id, is_active=True)
        if "courier" in form.base_fields:
            form.base_fields["courier"].queryset = User.objects.filter(is_active=True, profile__role="courier")
        return form

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        # Keep the order locked through form validation/save_related. A payment
        # webhook cannot make the form's unpaid snapshot stale before save.
        with transaction.atomic():
            if request.method == "POST" and object_id:
                try:
                    list(Order.objects.select_for_update(of=("self",)).filter(pk=int(object_id)))
                except (TypeError, ValueError):
                    pass
            return super().changeform_view(request, object_id, form_url, extra_context)

    def _status_choices(self, request, obj):
        if not obj or not self.has_change_permission(request, obj) or order_service.has_active_payment(obj):
            return []
        paid = bool(obj.paid_at or obj.payments.filter(status="succeeded").exists())
        return [
            (target, dict(Order.STATUS_CHOICES)[target])
            for target in self.transition_labels
            if obj.can_transition_to(target)
            and not (target == "confirmed" and obj.status == "adjusted")
            and not (paid and target in ("confirmed", "rejected", "cancelled"))
        ]

    def change_view(self, request, object_id, form_url="", extra_context=None):
        obj = self.get_object(request, object_id)
        context = dict(extra_context or {})
        editable = order_service.is_editable_order(obj) and self.has_change_permission(request, obj)
        context["can_adjust_items"] = editable
        choices = self._status_choices(request, obj)
        context["order_status_form"] = OrderStatusCommandForm(choices=choices)
        context["can_change_order_status"] = bool(choices)

        context["order_transition_buttons"] = [
            {"label": label, "url": reverse("admin:api_order_transition", args=[obj.pk, target])}
            for target, label in self.transition_labels.items()
            if obj and self.has_change_permission(request, obj) and obj.can_transition_to(target)
            and not (target == "confirmed" and obj.status == "adjusted")
            and not order_service.has_active_payment(obj)
        ]
        if not editable:
            context.update(show_save=False, show_save_and_continue=False, show_save_and_add_another=False)
        return super().change_view(request, object_id, form_url, context)

    def save_model(self, request, obj, form, change):
        changes = {field: form.cleaned_data[field] for field in order_service.METADATA_FIELDS if field in form.cleaned_data}
        saved = order_service.update_metadata(obj.pk, changes, actor=request.user,
            reason=form.cleaned_data.get("adjustment_reason") or "Изменение клиента через Django Admin")
        obj.__dict__.update(saved.__dict__)

    def get_urls(self):
        custom = [
            path("<int:object_id>/status/", self.admin_site.admin_view(self.set_status_view), name="api_order_set_status"),
            path("<int:object_id>/stores/", self.admin_site.admin_view(self.stores_view), name="api_order_stores"),
            path("<int:object_id>/adjust-items/", self.admin_site.admin_view(self.adjust_items_view), name="api_order_adjust_items"),
            path("<int:object_id>/transition/<str:target>/", self.admin_site.admin_view(self.transition_view), name="api_order_transition"),
        ]
        return custom + super().get_urls()

    def _editable_object(self, request, object_id):
        order = self.get_object(request, object_id)
        if order is None or not self.has_change_permission(request, order):
            raise PermissionDenied
        return order

    def stores_view(self, request, object_id):
        order = self._editable_object(request, object_id)
        if not order_service.is_editable_order(order):
            raise PermissionDenied
        try:
            client_id = int(request.GET.get("client", ""))
        except (TypeError, ValueError):
            return JsonResponse({"stores": []}, status=400)
        if not self._client_queryset(request, order).filter(pk=client_id).exists():
            raise PermissionDenied
        return JsonResponse({"stores": list(Store.objects.filter(client_id=client_id, is_active=True)
                                            .values("id", "name"))})

    def adjust_items_view(self, request, object_id):
        order = self._editable_object(request, object_id)
        if not order_service.is_editable_order(order):
            self.message_user(request, "Состав этого заказа недоступен для изменения", messages.ERROR)
            return redirect("admin:api_order_change", order.pk)
        initial = [{"item": item.pk, "product": item.product_id, "quantity": item.quantity} for item in order.items.order_by("pk")]
        formset = OrderItemCommandFormSet(request.POST if request.method == "POST" else None,
            initial=initial, form_kwargs={"order": order}, prefix="items")
        reason = OrderMutationReasonForm(request.POST if request.method == "POST" else None)
        error = None
        if request.method == "POST" and formset.is_valid() and reason.is_valid():
            payload = formset.command_payload(reason.cleaned_data["reason"])
            try:
                order_service.adjust_order(order.pk, payload, actor=request.user, source="admin")
            except order_service.OrderError as exc:
                error = exc.detail
            else:
                self.message_user(request, "Состав скорректирован. Клиент должен согласовать изменения.", messages.SUCCESS)
                return redirect("admin:api_order_change", order.pk)
        return TemplateResponse(request, "admin/api/order/adjust_items.html", {
            **self.admin_site.each_context(request), "opts": self.model._meta, "original": order,
            "title": f"Корректировка заказа #{order.pk}", "formset": formset, "reason_form": reason,
            "error": error, "media": self.media,
        })

    @method_decorator(require_POST)
    def set_status_view(self, request, object_id):
        order = self._editable_object(request, object_id)
        form = OrderStatusCommandForm(request.POST, choices=self._status_choices(request, order))
        if form.is_valid():
            try:
                order_service.transition_order(order.pk, form.cleaned_data["status"], actor=request.user,
                    source="admin", reason=form.cleaned_data["reason"])
            except order_service.OrderError as exc:
                form.add_error(None, exc.detail)
            else:
                self.message_user(request, "Статус заказа изменён", messages.SUCCESS)
                return redirect("admin:api_order_change", order.pk)
        return TemplateResponse(request, "admin/api/order/transition.html", {
            **self.admin_site.each_context(request), "opts": self.model._meta, "original": order,
            "title": "Изменить статус заказа", "form": form,
        })

    def transition_view(self, request, object_id, target):
        order = self._editable_object(request, object_id)
        if target not in self.transition_labels:
            raise PermissionDenied
        form = OrderMutationReasonForm(request.POST if request.method == "POST" else None)
        error = None
        if request.method == "POST" and form.is_valid():
            try:
                order_service.transition_order(order.pk, target, actor=request.user, source="admin", reason=form.cleaned_data["reason"])
            except order_service.OrderError as exc:
                error = exc.detail
            else:
                self.message_user(request, "Статус заказа изменён", messages.SUCCESS)
                return redirect("admin:api_order_change", order.pk)
        return TemplateResponse(request, "admin/api/order/transition.html", {
            **self.admin_site.each_context(request), "opts": self.model._meta, "original": order,
            "title": self.transition_labels[target], "form": form, "error": error,
        })

    def _transition_selected(self, request, queryset, target):
        for order in queryset:
            if not self.has_change_permission(request, order):
                raise PermissionDenied
            try:
                order_service.transition_order(order.pk, target, actor=request.user, source="admin",
                    reason=order.rejection_reason or "Действие Django Admin")
            except order_service.OrderError as exc:
                self.message_user(request, f"Заказ #{order.pk}: {exc.detail}", messages.ERROR)
            else:
                self.message_user(request, f"Заказ #{order.pk}: {self.transition_labels[target]}", messages.SUCCESS)

    @admin.action(description="Подтвердить выбранные заказы")
    def confirm_selected(self, request, queryset):
        self._transition_selected(request, queryset, "confirmed")

    @admin.action(description="Отклонить выбранные заказы")
    def reject_selected(self, request, queryset):
        self._transition_selected(request, queryset, "rejected")

    @admin.action(description="Отменить выбранные заказы")
    def cancel_selected(self, request, queryset):
        self._transition_selected(request, queryset, "cancelled")

    @admin.action(description="Отметить отправку выбранных заказов")
    def ship_selected(self, request, queryset):
        self._transition_selected(request, queryset, "shipped")

    @admin.action(description="Завершить выбранные заказы")
    def fulfill_selected(self, request, queryset):
        self._transition_selected(request, queryset, "fulfilled")


@admin.register(OrderAdjustment)
class OrderAdjustmentAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "created_by", "created_at")
    search_fields = ("order__id", "reason")
    readonly_fields = tuple(field.name for field in OrderAdjustment._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "provider", "provider_payment_id", "amount", "currency", "status", "created_at", "paid_at")
    list_filter = ("provider", "status")
    search_fields = ("order__id", "provider_payment_id")
    readonly_fields = tuple(field.name for field in Payment._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    list_display = ("id", "document_number", "client", "client_region", "distributor", "date", "total_amount", "status", "document_file")
    # Регион и категория клиента — основные разрезы аналитики, без них
    # приходилось выгружать всё и фильтровать в Excel руками.
    list_filter = ("status", "distributor", "client__region", "client__category", "date")
    search_fields = (
        "document_number",
        "client__company_name",
        "client__inn",
        # Поиск по артикулу: «кто и когда брал этот товар».
        "items__sku",
        "items__name",
    )
    date_hierarchy = "date"
    readonly_fields = ("created_at",)
    inlines = (PurchaseItemInline,)
    actions = ("export_xlsx", "export_csv")

    def get_queryset(self, request):
        # Список читает регион и дистрибьютора у каждой строки — иначе N+1.
        return super().get_queryset(request).select_related(
            "client", "client__region", "distributor"
        )

    @admin.display(description="Регион", ordering="client__region__name")
    def client_region(self, obj):
        return obj.client.region.name if obj.client and obj.client.region_id else "—"

    def _export(self, request, queryset, fmt):
        """Выгрузка отмеченных покупок позициями — с артикулами."""
        from .services import purchase_analytics as analytics
        from .services.exports import ExportUnavailable

        # Документы уже отобраны галочками — фильтры отчёта здесь не нужны.
        filters = analytics.Filters()
        try:
            return analytics.export_queryset(queryset, filters, fmt)
        except ExportUnavailable as exc:
            self.message_user(request, str(exc), messages.ERROR)

    @admin.action(description="Скачать в Excel (позициями)")
    def export_xlsx(self, request, queryset):
        return self._export(request, queryset, "xlsx")

    @admin.action(description="Скачать в CSV (позициями)")
    def export_csv(self, request, queryset):
        return self._export(request, queryset, "csv")


@admin.register(ColorRequest)
class ColorRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "client", "car_brand", "car_model", "color_code", "paint_type", "urgent", "transfer_method", "status", "created_at")
    list_filter = ("status", "paint_type", "urgent", "transfer_method", "client__distributor")
    search_fields = ("client__company_name", "vin", "car_brand", "car_model", "color_code", "color_name")
    readonly_fields = ("created_at",)


@admin.register(CourierTask)
class CourierTaskAdmin(admin.ModelAdmin):
    list_display = ("id", "client", "task_type", "address", "time_slot", "status", "courier", "created_at")
    list_filter = ("task_type", "status", "courier", "created_at", "client__distributor")
    search_fields = ("client__company_name", "address", "courier__username", "comment", "courier_comment")
    readonly_fields = ("created_at",)


@admin.register(Referral)
class ReferralAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "inviter",
        "invitee_name",
        "invitee_inn",
        "region",
        "is_registered",
        "has_purchase",
        "purchase_amount",
        "condition_met",
        "gift_status",
    )
    # gift_status остался колонкой ради истории старых подарков, но фильтровать
    # по нему нечего: у новых связок он всегда "none".
    list_filter = ("region", "is_registered", "has_purchase", "condition_met")
    search_fields = ("inviter__company_name", "invitee_name", "invitee_inn")
    readonly_fields = ("created_at", "gift_decided_at")


@admin.register(ExpertTicket)
class ExpertTicketAdmin(admin.ModelAdmin):
    list_display = ("id", "client", "category", "risk", "status", "created_at")
    list_filter = ("status", "category", "risk")
    search_fields = ("client__company_name", "question", "expert_answer")
    readonly_fields = ("created_at", "updated_at")


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("id", "client", "title", "type", "is_read", "created_at")
    list_filter = ("type", "is_read", "client__distributor")
    search_fields = ("client__company_name", "title", "body")
    readonly_fields = ("created_at",)


@admin.register(KnowledgeCard)
class KnowledgeCardAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "problem", "category", "status", "created_at")
    list_filter = ("status", "category")
    search_fields = ("title", "problem", "causes", "solution", "skus")
    readonly_fields = ("created_at", "updated_at")


IFRAME_SRC_RE = re.compile(r"""src\s*=\s*["']([^"']+)["']""", re.I)


def extract_url(value):
    """Достаёт адрес из того, что реально вставляют в поле.

    YouTube по кнопке «Поделиться → Встроить» отдаёт целый <iframe>, и вставить
    его в поле ссылки — самая частая ошибка. Вытаскиваем оттуда src вместо того,
    чтобы отбивать форму ошибкой валидации.
    """
    text = (value or "").strip()
    if not text:
        return text

    match = IFRAME_SRC_RE.search(text)
    if match:
        text = match.group(1).strip()

    # Протокол-относительные ссылки (//youtube.com/...) сами по себе валидны
    # в HTML, но URLField их не примет.
    if text.startswith("//"):
        text = f"https:{text}"
    return text


class NormalizingURLField(forms.URLField):
    """URL-поле, которое сначала чистит ввод, а потом уже проверяет."""

    def to_python(self, value):
        return super().to_python(extract_url(value))


class LearningMaterialForm(forms.ModelForm):
    class Meta:
        model = LearningMaterial
        fields = "__all__"
        field_classes = {
            "video_url": NormalizingURLField,
            "file_url": NormalizingURLField,
        }
        help_texts = {
            "video_url": (
                "Обычная ссылка на видео, например "
                "https://www.youtube.com/watch?v=XXXXXXXXXXX. "
                "Код <iframe> тоже можно вставить — ссылка из него возьмётся сама."
            ),
            "file_url": "Прямая ссылка на файл: PDF, документ, изображение.",
        }


@admin.register(BonusTransaction)
class BonusTransactionAdmin(admin.ModelAdmin):
    """Реестр бонусов. Это деньги — записи только читаются, не правятся."""

    list_display = ("id", "client", "amount", "kind", "order", "comment", "created_at")
    list_filter = ("kind", "created_at")
    search_fields = ("client__company_name", "client__inn", "comment")
    readonly_fields = ("created_at",)
    change_list_template = "admin/api/bonus_list.html"

    def get_urls(self):
        return [path("debit/", self.admin_site.admin_view(self.debit_view), name="api_bonus_debit")] + super().get_urls()

    def debit_view(self, request):
        if not self.has_add_permission(request):
            raise PermissionDenied
        from .services.bonuses import balance, manual_debit
        preview = request.method == "POST" and request.POST.get("preview")
        form = BonusDebitForm(initial=request.POST if preview else None) if preview else BonusDebitForm(request.POST or None)
        current = None
        if not preview and form.is_valid():
            current = balance(form.cleaned_data["client"])
            if request.method == "POST" and not request.POST.get("preview"):
                try:
                    manual_debit(form.cleaned_data["client"], form.cleaned_data["amount"], form.cleaned_data["reason"], request.user)
                except ValueError as exc:
                    form.add_error(None, str(exc))
                else:
                    self.message_user(request, "Бонусы списаны")
                    return redirect("admin:api_bonustransaction_changelist")
        elif request.POST.get("client", "").isdigit():
            client = ClientProfile.objects.filter(pk=request.POST["client"]).first()
            if client:
                current = balance(client)
        return TemplateResponse(request, "admin/api/bonus_debit.html", dict(self.admin_site.each_context(request), form=form, balance=current, title="Списать бонусы"))

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def add_view(self, request, form_url="", extra_context=None):
        return redirect("admin:api_bonus_debit")


@admin.register(LearningMaterial)
class LearningMaterialAdmin(admin.ModelAdmin):
    """Материалы ведутся отсюда: по п. 10 ТЗ клиент их только читает."""

    form = LearningMaterialForm
    list_display = ("id", "title", "kind", "category", "status", "created_at")
    list_filter = ("status", "kind", "category")
    search_fields = ("title", "summary", "body", "category")
    readonly_fields = ("created_at", "updated_at")


@admin.register(AuthToken)
class AuthTokenAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "key", "created_at")
    search_fields = ("user__username", "user__email", "key")
    readonly_fields = ("created_at",)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path("dashboard/", self.admin_site.admin_view(self.dashboard_view), name="api_dashboard"),
            path(
                "purchases-analytics/",
                self.admin_site.admin_view(self.purchase_analytics_view),
                name="api_purchase_analytics",
            ),
        ]
        return custom_urls + urls

    def purchase_analytics_view(self, request):
        """Аналитика по закупкам: срезы и выгрузка с одними фильтрами.

        Считает тот же сервис, что и приложение менеджера, — иначе цифры в
        админке и в приложении со временем разойдутся.
        """
        from .services import purchase_analytics as analytics
        from .services.exports import ExportUnavailable

        filters = analytics.Filters.from_request(request)

        export_format = request.GET.get("export")
        if export_format:
            try:
                return analytics.export(filters, export_format)
            except ExportUnavailable as exc:
                self.message_user(request, str(exc), messages.ERROR)

        data = analytics.report(filters)
        context = {
            **self.admin_site.each_context(request),
            "title": "Аналитика закупок",
            "filters": filters.as_dict(),
            "regions": Region.objects.filter(is_active=True).order_by("name"),
            "distributors": Distributor.objects.filter(is_active=True).order_by("name"),
            "statuses": Purchase.STATUS_CHOICES,
            **data,
        }
        return render(request, "admin/api/purchase_analytics.html", context)

    def dashboard_view(self, request):
        from django.db.models import Count
        from django.db.models.functions import TruncMonth
        from .models import ClientProfile, Purchase, Order, ExpertTicket, ColorRequest, Referral, Region, Distributor

        # Filters
        days = int(request.GET.get("days", 30))
        region_id = request.GET.get("region")
        distributor_id = request.GET.get("distributor")

        start_date = timezone.now() - timezone.timedelta(days=days)

        # Base Querysets
        clients_qs = ClientProfile.objects.all()
        purchases_qs = Purchase.objects.all()
        orders_qs = Order.objects.all()
        tickets_qs = ExpertTicket.objects.all()
        color_qs = ColorRequest.objects.all()
        referrals_qs = Referral.objects.all()

        if region_id:
            # Регион — внешний ключ: фильтруем по id. Раньше здесь стояло имя
            # региона, отчего запрос падал с ValueError, исключение
            # проглатывалось, и фильтр молча не применялся.
            try:
                region_pk = int(region_id)
            except (TypeError, ValueError):
                region_pk = None
            if region_pk is not None:
                clients_qs = clients_qs.filter(region_id=region_pk)
                purchases_qs = purchases_qs.filter(client__region_id=region_pk)
                orders_qs = orders_qs.filter(client__region_id=region_pk)
                tickets_qs = tickets_qs.filter(client__region_id=region_pk)
                color_qs = color_qs.filter(client__region_id=region_pk)
                referrals_qs = referrals_qs.filter(inviter__region_id=region_pk)

        if distributor_id:
            clients_qs = clients_qs.filter(distributor_id=distributor_id)
            purchases_qs = purchases_qs.filter(distributor_id=distributor_id)
            orders_qs = orders_qs.filter(distributor_id=distributor_id)
            tickets_qs = tickets_qs.filter(client__distributor_id=distributor_id)
            color_qs = color_qs.filter(client__distributor_id=distributor_id)
            referrals_qs = referrals_qs.filter(inviter__distributor_id=distributor_id)

        # KPI Metrics
        context = {
            **self.admin_site.each_context(request),
            "title": "Аналитика AutoTerra",
            "kpi": {
                "clients_total": clients_qs.count(),
                "new_registrations": clients_qs.filter(created_at__gte=start_date).count(),
                "purchases_count": purchases_qs.filter(created_at__gte=start_date).count(),
                "orders_count": orders_qs.filter(created_at__gte=start_date).count(),
                "tickets_count": tickets_qs.filter(created_at__gte=start_date).count(),
                "color_requests_count": color_qs.filter(created_at__gte=start_date).count(),
                "referrals_count": referrals_qs.filter(created_at__gte=start_date).count(),
            },
            "regions": Region.objects.filter(is_active=True),
            "distributors": Distributor.objects.filter(is_active=True),
            "current_filters": {
                "days": days,
                "region": region_id,
                "distributor": distributor_id,
            }
        }

        # Charts Data
        reg_trend = (clients_qs.filter(created_at__gte=timezone.now() - timezone.timedelta(days=180))
                    .annotate(month=TruncMonth("created_at"))
                    .values("month")
                    .annotate(count=Count("id"))
                    .order_by("month"))
        
        pur_trend = (purchases_qs.filter(created_at__gte=timezone.now() - timezone.timedelta(days=180))
                    .annotate(month=TruncMonth("created_at"))
                    .values("month")
                    .annotate(count=Count("id"))
                    .order_by("month"))

        context["chart_labels"] = [i["month"].strftime("%b %Y") for i in reg_trend]
        context["reg_data"] = [i["count"] for i in reg_trend]
        context["pur_data"] = [i["count"] for i in pur_trend]

        # Regional Activity
        # values("region") дал бы id региона — на подписях осей нужно название.
        reg_activity = (
            clients_qs.values("region__name").annotate(count=Count("id")).order_by("-count")[:10]
        )
        context["reg_activity_labels"] = [i["region__name"] or "—" for i in reg_activity]
        context["reg_activity_data"] = [i["count"] for i in reg_activity]

        return render(request, "admin/api/dashboard.html", context)


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    list_display = ("id", "file", "file_type", "uploaded_by", "uploaded_at", "content_type", "object_id")
    list_filter = ("file_type", "content_type", "uploaded_at")
    search_fields = ("file", "description", "uploaded_by__username", "uploaded_by__email")
    readonly_fields = ("uploaded_at",)


@admin.register(PartnerTier)
class PartnerTierAdmin(admin.ModelAdmin):
    """Ранги и пороги: с какого оборота клиент получает какой ранг."""

    list_display = ("name", "threshold_display", "clients_count", "discounts_count", "is_active")
    list_editable = ("is_active",)
    search_fields = ("name", "description")
    ordering = ("threshold",)
    actions = ("recalculate_tiers",)
    fieldsets = (
        (None, {
            "fields": ("name", "threshold", "description", "is_active"),
            "description": (
                "Порог — оборот клиента: подтверждённые закупки плюс оплаченные заказы. "
                "Ранг присваивается автоматически при подтверждении закупки и при оплате "
                "заказа. Понижения не происходит — для этого есть действие «Пересчитать "
                "ранги» внизу списка клиентов."
            ),
        }),
    )

    @admin.display(description="Порог оборота", ordering="threshold")
    def threshold_display(self, obj):
        return format_html("<b>от {} ₽</b>", f"{obj.threshold:,.0f}".replace(",", " "))

    @admin.display(description="Клиентов")
    def clients_count(self, obj):
        return ClientProfile.objects.filter(partner_status=obj.name).count()

    @admin.display(description="Правил скидок")
    def discounts_count(self, obj):
        return obj.discounts.count()

    @admin.action(description="Пересчитать ранги клиентов по обороту (без понижения)")
    def recalculate_tiers(self, request, queryset):
        from .services.tiers import recalculate_all

        stats = recalculate_all(allow_downgrade=False)
        self.message_user(
            request,
            f"Проверено клиентов: {stats['checked']}, повышено: {stats['upgraded']}.",
            messages.SUCCESS,
        )


@admin.register(RankDiscount)
class RankDiscountAdmin(admin.ModelAdmin):
    """Прайс по рангам: дилерский салон платит меньше гаражного сервиса."""

    list_display = (
        "tier",
        "scope_display",
        "percent_display",
        "distributor",
        "is_active",
        "example",
        "updated_at",
    )
    list_filter = ("tier", "is_active", "distributor")
    search_fields = ("product_category", "comment")
    list_editable = ("is_active",)
    readonly_fields = ("updated_at",)
    fieldsets = (
        ("Кому", {
            "fields": ("tier", "distributor"),
            "description": (
                "Ранг клиент получает автоматически по обороту — пороги "
                "настраиваются в разделе «Ранги клиентов». "
                "Дистрибьютор пустой — правило работает у всех."
            ),
        }),
        ("На что", {
            "fields": ("product_category",),
            "description": (
                "Категория товаров как в ассортименте. Пусто — скидка на весь прайс. "
                "Правило на конкретную категорию перебивает правило на весь прайс."
            ),
        }),
        ("Сколько", {"fields": ("percent", "is_active", "comment", "updated_at")}),
    )

    @admin.display(description="На что")
    def scope_display(self, obj):
        return obj.product_category or "весь ассортимент"

    @admin.display(description="Скидка", ordering="percent")
    def percent_display(self, obj):
        return format_html("<b>−{}%</b>", obj.percent)

    @admin.display(description="Пример: 10 000 ₽ →")
    def example(self, obj):
        from .services.pricing import apply_discount

        return f"{apply_discount(10000, obj.percent):,.0f} ₽".replace(",", " ")

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        # Подсказываем реальные категории из ассортимента, чтобы правило не
        # завели на категорию с опечаткой — такое правило молча не сработает.
        if db_field.name == "product_category":
            categories = (
                Product.objects.exclude(category="")
                .order_by("category")
                .values_list("category", flat=True)
                .distinct()
            )
            listed = ", ".join(list(categories)[:12]) or "— ассортимент пуст"
            kwargs["help_text"] = f"Пусто — весь ассортимент. Категории в базе: {listed}"
        return super().formfield_for_dbfield(db_field, request, **kwargs)


admin.site.register(ManagerTask)
admin.site.register(ContactHistory)

# Keep AI audit/review screens separate from the existing business admin classes.
from . import ai_admin  # noqa: E402,F401


class BonusDebitForm(forms.Form):
    client = forms.ModelChoiceField(queryset=ClientProfile.objects.all(), label="Клиент")
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"), label="Сумма списания")
    reason = forms.CharField(max_length=255, label="Причина", widget=forms.Textarea)


from .models import AndroidRelease

@admin.register(AndroidRelease)
class AndroidReleaseAdmin(admin.ModelAdmin):
    list_display = ("store", "latest_version_name", "latest_build_number", "minimum_supported_build_number")


from .models import ReferralRuleVersion, ReferralAccrual

@admin.register(ReferralRuleVersion)
class ReferralRuleVersionAdmin(admin.ModelAdmin):
    list_display = ("effective_from", "tiers", "activity_min")
    def has_delete_permission(self, request, obj=None):
        return False
    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and (obj is None or obj.effective_from > timezone.now())

@admin.register(ReferralAccrual)
class ReferralAccrualAdmin(admin.ModelAdmin):
    list_display = ("id", "referral", "version", "confirmed_at", "turnover", "amount", "legacy_snapshot")
    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False
