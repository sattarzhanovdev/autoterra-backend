from decimal import Decimal, InvalidOperation
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from django.template.response import TemplateResponse
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils import timezone
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from .forms import ProductExcelImportForm
from .services.exports import EXPORTERS as EXPORT_FORMATS, ExportUnavailable, export_clients
from .models import (
    Attachment,
    AuthToken,
    ClientProfile,
    ColorRequest,
    ContactHistory,
    CourierTask,
    Distributor,
    ExpertTicket,
    KnowledgeCard,
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


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    fields = ("product", "sku", "name", "quantity", "price", "brand", "category", "volume")
    readonly_fields = ("sku", "name", "brand", "category", "volume")


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


@admin.register(Region)
class RegionAdmin(admin.ModelAdmin):
    list_display = ("id", "code", "name", "distributor", "manager", "is_active")
    list_filter = ("is_active", "distributor")
    search_fields = ("code", "name", "distributor__name", "manager__username", "manager__email")


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
    readonly_fields = ("created_at",)
    inlines = (StoreInline,)
    actions = ("export_xlsx", "export_docx", "export_pdf", "export_csv")
    change_list_template = "admin/api/clientprofile/change_list.html"

    def get_queryset(self, request):
        # Выгрузка читает регион, дистрибьютора и менеджера у каждого клиента —
        # без select_related это N+1 на весь список.
        return (
            super().get_queryset(request)
            .select_related("user", "region", "distributor", "manager")
        )

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


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    change_list_template = "admin/api/product/change_list.html"
    list_display = ("id", "sku", "external_id", "name", "category", "brand", "distributor", "quantity", "status", "price", "photo_preview")
    list_filter = ("status", "category", "brand", "distributor")
    search_fields = ("sku", "external_id", "name", "category", "brand", "distributor__name")
    readonly_fields = ("updated_at", "photo_preview")

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
        }
        for column, width in widths.items():
            sheet.column_dimensions[column].width = width

        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = "A1:I2"
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


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("id", "client", "store", "distributor", "external_id", "status", "created_at")
    list_filter = ("status", "distributor", "store")
    search_fields = ("id", "external_id", "client__company_name", "client__inn", "store__name", "comment", "items__name", "items__sku")
    readonly_fields = ("created_at", "confirmed_at", "paid_at", "shipped_at")
    inlines = (OrderItemInline,)


@admin.register(OrderAdjustment)
class OrderAdjustmentAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "created_by", "created_at")
    search_fields = ("order__id", "reason")
    readonly_fields = ("created_at",)


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "provider", "provider_payment_id", "amount", "currency", "status", "created_at", "paid_at")
    list_filter = ("provider", "status")
    search_fields = ("order__id", "provider_payment_id")
    readonly_fields = ("created_at", "paid_at", "raw_response")


@admin.register(Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    list_display = ("id", "document_number", "client", "distributor", "date", "total_amount", "status", "document_file")
    list_filter = ("status", "distributor", "date")
    search_fields = ("document_number", "client__company_name", "client__inn")
    readonly_fields = ("created_at",)
    inlines = (PurchaseItemInline,)


@admin.register(ColorRequest)
class ColorRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "client", "car_brand", "car_model", "color_code", "urgent", "transfer_method", "status", "created_at")
    list_filter = ("status", "urgent", "transfer_method", "client__distributor")
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
    )
    list_filter = ("region", "is_registered", "has_purchase", "condition_met")
    search_fields = ("inviter__company_name", "invitee_name", "invitee_inn")
    readonly_fields = ("created_at",)


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


@admin.register(AuthToken)
class AuthTokenAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "key", "created_at")
    search_fields = ("user__username", "user__email", "key")
    readonly_fields = ("created_at",)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path("dashboard/", self.admin_site.admin_view(self.dashboard_view), name="api_dashboard"),
        ]
        return custom_urls + urls

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
            try:
                reg_name = Region.objects.get(id=region_id).name
                clients_qs = clients_qs.filter(region=reg_name)
                purchases_qs = purchases_qs.filter(client__region=reg_name)
                orders_qs = orders_qs.filter(client__region=reg_name)
                tickets_qs = tickets_qs.filter(client__region=reg_name)
                color_qs = color_qs.filter(client__region=reg_name)
                referrals_qs = referrals_qs.filter(inviter__region=reg_name)
            except (Region.DoesNotExist, ValueError):
                pass

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
        reg_activity = clients_qs.values("region").annotate(count=Count("id")).order_by("-count")[:10]
        context["reg_activity_labels"] = [i["region"] for i in reg_activity]
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
