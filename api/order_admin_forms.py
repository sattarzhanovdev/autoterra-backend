"""Admin forms submit commands; they never persist OrderItem model forms."""
from django import forms
from django.forms import BaseFormSet, formset_factory

from api.models import Order, OrderItem, Product
from api.services import orders


class OrderAdminForm(forms.ModelForm):
    adjustment_reason = forms.CharField(label='Причина изменения клиента / цен', required=False,
        widget=forms.Textarea(attrs={'rows': 2}),
        help_text='При смене клиента персональные цены пересчитываются, а состав направляется ему на согласование.')

    class Meta:
        model = Order
        fields = '__all__'

    class Media:
        js = ('admin/order_edit.js',)

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk:
            changes = {field: cleaned[field] for field in orders.METADATA_FIELDS if field in cleaned}
            try:
                current = Order.objects.select_related('client', 'client__user', 'distributor', 'store').get(pk=self.instance.pk)
                orders.validate_metadata(current, changes)
            except orders.OrderError as exc:
                raise forms.ValidationError(exc.detail) from exc
        return cleaned


class OrderItemCommandForm(forms.Form):
    item = forms.ModelChoiceField(queryset=OrderItem.objects.none(), required=False, widget=forms.HiddenInput)
    product = forms.ModelChoiceField(queryset=Product.objects.none(), required=False, label='Товар')
    quantity = forms.IntegerField(label='Количество', min_value=0, max_value=1000000, initial=1)

    def __init__(self, *args, order, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['item'].queryset = order.items.all()
        self.fields['product'].queryset = Product.objects.filter(distributor=order.distributor, is_active=True).order_by('name')
        existing = self.initial.get('item')
        if existing:
            # Existing product, price and identity are immutable here. To replace
            # a product, delete its row and add a new product through the service.
            self.fields['product'].disabled = True
            self.fields['product'].queryset = Product.objects.filter(pk=self.initial.get('product'))

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('DELETE'):
            return cleaned
        if not cleaned.get('item') and not cleaned.get('product'):
            raise forms.ValidationError('Выберите товар для новой позиции')
        if not cleaned.get('item') and cleaned.get('quantity') == 0:
            raise forms.ValidationError('Количество нового товара должно быть положительным')
        return cleaned


class OrderItemCommandFormSet(BaseFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        ids = [form.cleaned_data['item'].pk for form in self.forms if form.cleaned_data.get('item')]
        if len(ids) != len(set(ids)):
            raise forms.ValidationError('Позиция заказа передана несколько раз')

    def command_payload(self, reason):
        updates, additions = [], []
        for form in self.forms:
            row = form.cleaned_data
            if not row:
                continue
            item = row.get('item')
            if item:
                quantity = 0 if row.get('DELETE') else row['quantity']
                if quantity != item.quantity:
                    updates.append({'itemId': item.pk, 'quantity': quantity})
            elif row.get('product') and not row.get('DELETE'):
                additions.append({'productId': row['product'].pk, 'quantity': row['quantity']})
        return {'items': updates, 'newItems': additions, 'reason': reason}


OrderItemCommandFormSet = formset_factory(OrderItemCommandForm, formset=OrderItemCommandFormSet,
    extra=3, can_delete=True, max_num=500, validate_max=True, absolute_max=1000)


class OrderMutationReasonForm(forms.Form):
    reason = forms.CharField(label='Причина / комментарий', required=False,
                            widget=forms.Textarea(attrs={'rows': 3}))


class OrderStatusCommandForm(OrderMutationReasonForm):
    status = forms.ChoiceField(label='Новый статус')

    def __init__(self, *args, choices, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['status'].choices = choices
        self.order_fields(['status', 'reason'])
