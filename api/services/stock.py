"""Physical stock reservations. Call reserve/release with the product row locked."""
from django.db import transaction
from api.models import Order, Product


def _save(product):
    if product.status != 'onOrder':
        product.status = 'inStock' if product.quantity > 5 else 'low' if product.quantity else 'outOfStock'
    product.save(update_fields=['quantity', 'status'])


def reserve_product(product, quantity):
    reserved = min(product.quantity, quantity)
    product.quantity -= reserved
    _save(product)
    return reserved


def release_product(product, quantity):
    product.quantity += quantity
    _save(product)


@transaction.atomic
def restore_order_stock(order):
    locked = Order.objects.select_for_update().get(pk=order.pk)
    if locked.stock_restored:
        return False
    for item in locked.items.order_by('product_id', 'pk'):
        product = Product.objects.select_for_update().get(pk=item.product_id)
        release_product(product, item.reserved_quantity)
        item.reserved_quantity = 0
        item.save(update_fields=['reserved_quantity'])
    locked.stock_restored = True
    locked.save(update_fields=['stock_restored'])
    order.stock_restored = True
    return True
