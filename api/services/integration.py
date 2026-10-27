import logging
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from django.db import transaction
from ..models import Product, ClientProfile, Order, Distributor, normalize_product_images

logger = logging.getLogger(__name__)

class IntegrationStrategy(ABC):
    """
    Abstract base class for integration strategies (REST, OData, SOAP).
    Each strategy will implement actual data fetching from 1C.
    """
    @abstractmethod
    def fetch_products(self) -> List[Dict[str, Any]]:
        pass

    @abstractmethod
    def fetch_stock(self) -> List[Dict[str, Any]]:
        pass

    @abstractmethod
    def fetch_clients(self) -> List[Dict[str, Any]]:
        pass

    @abstractmethod
    def push_order(self, order_data: Dict[str, Any]) -> Dict[str, Any]:
        pass


class RestIntegrationStrategy(IntegrationStrategy):
    """
    Example implementation for 1C REST API.
    """
    def fetch_products(self) -> List[Dict[str, Any]]:
        # TODO: Implement 1C REST client
        return []

    def fetch_stock(self) -> List[Dict[str, Any]]:
        return []

    def fetch_clients(self) -> List[Dict[str, Any]]:
        return []

    def push_order(self, order_data: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "success", "external_id": "1C-ORD-EXAMPLE"}


class ODataIntegrationStrategy(IntegrationStrategy):
    """
    Example implementation for 1C OData.
    """
    def fetch_products(self) -> List[Dict[str, Any]]:
        return []

    def fetch_stock(self) -> List[Dict[str, Any]]:
        return []

    def fetch_clients(self) -> List[Dict[str, Any]]:
        return []

    def push_order(self, order_data: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "success"}


class SoapIntegrationStrategy(IntegrationStrategy):
    """
    Example implementation for 1C SOAP Web Services.
    """
    def fetch_products(self) -> List[Dict[str, Any]]:
        return []

    def fetch_stock(self) -> List[Dict[str, Any]]:
        return []

    def fetch_clients(self) -> List[Dict[str, Any]]:
        return []

    def push_order(self, order_data: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "success"}


class IntegrationService:
    """
    Service layer for 1C integration.
    Uses strategy pattern to handle different connection types.
    """
    def __init__(self, strategy: IntegrationStrategy):
        self.strategy = strategy

    def sync_products(self, distributor: Distributor, *, full_sync=False):
        from .catalog_sync import sync_catalog
        result = sync_catalog(distributor, self.strategy.fetch_products(), full_sync=full_sync)
        if result["errors"]:
            logger.error("Catalog sync errors: %s", result["errors"])
        return result["created"] + result["updated"]

    def sync_stock(self, distributor: Distributor):
        from .catalog_sync import sync_catalog
        result = sync_catalog(distributor, self.strategy.fetch_stock(), allow_create=False, sync_type="stock_update")
        if result["errors"]:
            logger.error("Stock sync errors: %s", result["errors"])
        return result["updated"]

    def sync_clients(self, distributor: Distributor):
        """
        Import or update client profiles from 1C.
        """
        data = self.strategy.fetch_clients()
        processed = 0
        for item in data:
            with transaction.atomic():
                ClientProfile.objects.update_or_create(
                    distributor=distributor,
                    external_id=item.get("external_id"),
                    defaults={
                        "company_name": item.get("name"),
                        "inn": item.get("inn"),
                        "region": item.get("region"),
                        "city": item.get("city"),
                        "phone": item.get("phone"),
                        "status": item.get("status", "active"),
                    }
                )
            processed += 1
        return processed

    def sync_orders(self, distributor: Distributor):
        """
        Push local orders to 1C and update statuses.
        """
        orders_to_sync = Order.objects.filter(
            distributor=distributor,
            external_id__isnull=True
        ).prefetch_related("items")

        processed = 0
        for order in orders_to_sync:
            order_data = {
                "id": order.id,
                "client_external_id": order.client.external_id,
                "items": [
                    {"sku": item.sku, "quantity": item.quantity, "price": item.price}
                    for item in order.items.all()
                ],
                "comment": order.comment,
            }
            
            result = self.strategy.push_order(order_data)
            if result.get("status") == "success":
                order.external_id = result.get("external_id")
                order.save(update_fields=["external_id"])
                processed += 1
        
        return processed
