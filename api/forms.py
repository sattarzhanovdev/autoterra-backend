from django import forms

from .models import Distributor


class ProductExcelImportForm(forms.Form):
    distributor = forms.ModelChoiceField(
        label="Дистрибьютор",
        queryset=Distributor.objects.filter(is_active=True),
        help_text="Все строки из файла будут загружены в ассортимент выбранного дистрибьютора.",
    )
    file = forms.FileField(
        label="Excel файл",
        help_text=(
            "Формат .xlsx. Первая строка — заголовки. Рекомендуемый формат: "
            "Артикул, Название, Категория, Бренд, Объём, Цена, Остаток, Статус. "
            "Также поддерживаются английские заголовки: Article, Product name, Category, "
            "Brand, Volume, Price, Stock, Status."
        ),
    )
