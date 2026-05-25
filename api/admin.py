from decimal import Decimal, InvalidOperation

from django.contrib import admin, messages
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils.html import format_html

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from .forms import ProductExcelImportForm
from .models import (
    AuthToken,
    ClientProfile,
    ColorRequest,
    CourierTask,
    Distributor,
    ExpertTicket,
    KnowledgeCard,
    Notification,
    Order,
    OrderItem,
    Product,
    Purchase,
    PurchaseItem,
    Referral,
    Store,
)

admin.site.site_header = "AutoTerra Admin"
admin.site.site_title = "AutoTerra"
admin.site.index_title = "Панель управления платформой"


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


@admin.register(Distributor)
class DistributorAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "inn", "user", "phone", "email", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "inn", "phone", "email", "user__username", "user__email")
    inlines = (ProductInline,)


@admin.register(ClientProfile)
class ClientProfileAdmin(admin.ModelAdmin):
    list_display = ("id", "company_name", "inn", "category", "region", "city", "distributor", "status", "partner_status")
    list_filter = ("category", "status", "partner_status", "region", "distributor")
    search_fields = ("company_name", "inn", "contact_name", "phone", "user__username", "user__email")
    readonly_fields = ("created_at",)
    inlines = (StoreInline,)


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "client", "address", "is_active", "created_at")
    list_filter = ("is_active", "client__region", "client__distributor")
    search_fields = ("name", "address", "client__company_name", "client__inn")
    readonly_fields = ("created_at",)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    change_list_template = "admin/api/product/change_list.html"
    list_display = ("id", "sku", "name", "category", "brand", "distributor", "quantity", "status", "price")
    list_filter = ("status", "category", "brand", "distributor")
    search_fields = ("sku", "name", "category", "brand", "distributor__name")
    readonly_fields = ("updated_at",)

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
        }
        for column, width in widths.items():
            sheet.column_dimensions[column].width = width

        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = "A1:H2"
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
                created, updated, skipped, errors = self._import_products(
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
                    f"Импорт завершён: создано {created}, обновлено {updated}, пропущено {skipped}.",
                )
                return redirect("admin:api_product_changelist")
        else:
            form = ProductExcelImportForm()

        context = {
            **self.admin_site.each_context(request),
            "title": "Загрузка ассортимента из Excel",
            "form": form,
            "opts": self.model._meta,
            "sample_headers_ru": ", ".join(PRODUCT_TEMPLATE_HEADERS),
            "sample_headers_en": "Article, Product name, Category, Brand, Volume, Price, Stock, Status",
            "statuses": "В наличии / Мало / Под заказ / Нет в наличии",
        }
        return render(request, "admin/api/product/import_excel.html", context)

    def _import_products(self, distributor, file_obj):
        workbook = load_workbook(file_obj, read_only=True, data_only=True)
        sheet = workbook.active
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            return 0, 0, 0, ["Файл пустой."]

        headers = _headers(rows[0])
        required = {"sku", "name"}
        missing = required - set(headers)
        if missing:
            return 0, 0, 0, [f"Не найдены обязательные колонки: {', '.join(sorted(missing))}."]

        created = updated = skipped = 0
        errors = []
        for number, row in enumerate(rows[1:], start=2):
            sku = _norm(_cell(row, headers, "sku"))
            name = _norm(_cell(row, headers, "name"))
            if not sku and not name:
                skipped += 1
                continue
            if not sku or not name:
                skipped += 1
                errors.append(f"Строка {number}: артикул и название обязательны.")
                continue

            defaults = {
                "name": name,
                "category": _norm(_cell(row, headers, "category", "Без категории")) or "Без категории",
                "brand": _norm(_cell(row, headers, "brand", "AutoTerra")) or "AutoTerra",
                "volume": _money(_cell(row, headers, "volume", 0)),
                "price": _money(_cell(row, headers, "price", 0)),
                "quantity": max(_int(_cell(row, headers, "quantity", 0)), 0),
                "status": _status(_cell(row, headers, "status", "inStock")),
                "is_active": True,
            }
            _, was_created = Product.objects.update_or_create(
                distributor=distributor,
                sku=sku,
                defaults=defaults,
            )
            if was_created:
                created += 1
            else:
                updated += 1

        return created, updated, skipped, errors


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("id", "client", "store", "distributor", "status", "created_at")
    list_filter = ("status", "distributor", "store")
    search_fields = ("client__company_name", "client__inn", "store__name", "comment", "items__name", "items__sku")
    readonly_fields = ("created_at",)
    inlines = (OrderItemInline,)


@admin.register(Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    list_display = ("id", "document_number", "client", "distributor", "date", "total_amount", "status")
    list_filter = ("status", "distributor", "date")
    search_fields = ("document_number", "client__company_name", "client__inn")
    readonly_fields = ("created_at",)
    inlines = (PurchaseItemInline,)


@admin.register(ColorRequest)
class ColorRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "client", "car_brand", "car_model", "color_code", "urgent", "courier_pickup", "status", "created_at")
    list_filter = ("status", "urgent", "courier_pickup", "client__distributor")
    search_fields = ("client__company_name", "vin", "car_brand", "car_model", "color_code", "color_name")
    readonly_fields = ("created_at",)


@admin.register(CourierTask)
class CourierTaskAdmin(admin.ModelAdmin):
    list_display = ("id", "client", "type", "address", "scheduled_time", "status", "courier_id")
    list_filter = ("type", "status", "scheduled_time", "client__distributor")
    search_fields = ("client__company_name", "address", "contact_name", "contact_phone", "car_description")
    readonly_fields = ("created_at",)


@admin.register(Referral)
class ReferralAdmin(admin.ModelAdmin):
    list_display = ("id", "inviter", "invitee_name", "invitee_inn", "region", "is_registered", "has_purchase", "condition_met")
    list_filter = ("region", "is_registered", "has_purchase", "condition_met")
    search_fields = ("inviter__company_name", "invitee_name", "invitee_inn")
    readonly_fields = ("created_at",)


@admin.register(ExpertTicket)
class ExpertTicketAdmin(admin.ModelAdmin):
    list_display = ("id", "client", "category", "status", "created_at")
    list_filter = ("status", "category")
    search_fields = ("client__company_name", "question", "ai_answer", "expert_answer")
    readonly_fields = ("created_at",)


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("id", "client", "title", "type", "is_read", "created_at")
    list_filter = ("type", "is_read", "client__distributor")
    search_fields = ("client__company_name", "title", "body")
    readonly_fields = ("created_at",)


@admin.register(KnowledgeCard)
class KnowledgeCardAdmin(admin.ModelAdmin):
    list_display = ("id", "problem", "approving_expert", "is_approved", "created_at")
    list_filter = ("is_approved",)
    search_fields = ("problem", "causes", "solution", "skus")
    readonly_fields = ("created_at",)


@admin.register(AuthToken)
class AuthTokenAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "key", "created_at")
    search_fields = ("user__username", "user__email", "key")
    readonly_fields = ("created_at",)
