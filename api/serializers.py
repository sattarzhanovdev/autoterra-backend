import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from .models import ClientProfile, Region, Purchase, Distributor

class BaseSerializer:
    def __init__(self, data):
        self.data = data
        self.errors = {}
        self.validated_data = {}

    def is_valid(self):
        raise NotImplementedError()

class RegistrationSerializer(BaseSerializer):
    def is_valid(self):
        username = self.data.get('username')
        password = self.data.get('password')
        inn_raw = self.data.get('inn', '')
        # Нормализация ИНН: оставляем только цифры
        inn = "".join(ch for ch in str(inn_raw) if ch.isdigit())
        
        region_id = self.data.get('region_id')
        company_name = self.data.get('company_name', '').strip()
        contact_name = self.data.get('contact_name', '').strip()

        if not username:
            self.errors['username'] = "Введите номер телефона"
        if not password or len(password) < 8:
            self.errors['password'] = "Пароль должен быть не менее 8 символов"
        if not inn or len(inn) not in (10, 12):
            self.errors['inn'] = "ИНН должен состоять из 10 или 12 цифр"
        if not region_id:
            self.errors['region_id'] = "Выберите регион"
        if not company_name:
            self.errors['company_name'] = "Введите название компании"
        if not contact_name:
            self.errors['contact_name'] = "Введите ФИО контактного лица"

        if self.errors:
            return False

        try:
            region = Region.objects.get(id=region_id)
        except (Region.DoesNotExist, ValueError):
            self.errors['region_id'] = "Регион не найден"
            return False

        self.validated_data = {
            'username': username,
            'password': password,
            'inn': inn,
            'region': region,
            'company_name': company_name,
            'contact_name': contact_name,
        }
        return True

class PurchaseSerializer(BaseSerializer):
    def is_valid(self):
        doc_number = self.data.get('document_number', '').strip()
        date_str = self.data.get('date')
        amount_str = self.data.get('amount')

        if not doc_number:
            self.errors['document_number'] = "Document number is required"
        if not date_str:
            self.errors['date'] = "Date is required"
        if not amount_str:
            self.errors['amount'] = "Amount is required"

        if self.errors:
            return False

        try:
            amount = Decimal(str(amount_str))
        except (InvalidOperation, ValueError):
            self.errors['amount'] = "Invalid amount format"
            return False

        try:
            parsed_date = datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
        except (ValueError, TypeError):
            self.errors['date'] = "Invalid date format"
            return False

        self.validated_data = {
            'document_number': doc_number,
            'date': parsed_date,
            'amount': amount,
        }
        return True

class CourierTaskSerializer(BaseSerializer):
    def is_valid(self):
        status = self.data.get('status')
        if status and status not in ["assigned", "in_progress", "delivered", "returned", "cancelled"]:
            self.errors['status'] = "Некорректный статус"
            return False
        
        self.validated_data = {
            'status': status,
            'courier_comment': self.data.get('courier_comment', '').strip(),
        }
        return True

def _format_courier_task(task):
    if not task:
        return None
    return {
        "id": str(task.id),
        "clientName": task.client.company_name,
        "type": task.task_type,
        "typeDisplay": task.get_task_type_display(),
        "address": task.address,
        "timeSlot": task.time_slot,
        "status": task.status,
        "statusDisplay": task.get_status_display(),
        "photoProof": task.proof_photo.url if task.proof_photo else None,
        "comment": task.comment,
        "courierComment": task.courier_comment,
        "createdAt": task.created_at.isoformat(),
        "orderId": str(task.order.id) if task.order else None,
        "colorRequestId": str(task.color_request.id) if task.color_request else None,
    }
