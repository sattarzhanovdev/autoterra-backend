"""Staff-only audit/review surfaces; original chat messages are immutable here."""
from urllib.parse import urlparse

from django.contrib import admin
from django.db.models import Count, OuterRef, Q, Subquery
from django.urls import reverse
from django.utils.html import format_html, format_html_join

from .models import AIConversation, AIMessage, AIMessageFeedback, KnowledgeDocument, KnowledgeDriveFile
from .services.ai_history import save_feedback


def source_details(message):
    rows = []
    for source in message.sources:
        title = source.get('filename') or source.get('title') or source.get('id', '')
        location = ', '.join(str(value) for value in (
            f"стр. {source['page']}" if source.get('page') else '', source.get('sheet'),
            f"строки {source.get('rowStart')}–{source.get('rowEnd')}" if source.get('rowStart') else '',
            source.get('section')) if value)
        url = source.get('url', '')
        safe_url = (url.startswith('/') and not url.startswith('//')) or urlparse(url).scheme == 'https'
        link = format_html('<a href="{}" rel="noopener noreferrer">{}</a>', url, title) if url and safe_url else title
        rows.append((link, location, source.get('excerpt', '')))
    return format_html_join('', '<div><b>{}</b> {}<pre style="white-space:pre-wrap">{}</pre></div>', rows) or '—'


class MessageInline(admin.StackedInline):
    model = AIMessage
    fk_name = 'conversation'
    extra = 0
    can_delete = False
    ordering = ('pk',)
    fields = ('role', 'content', 'created_at', 'status', 'error', 'provider', 'model', 'latency_ms', 'materials', 'review')
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description='Использованные источники')
    def materials(self, obj):
        return source_details(obj)

    @admin.display(description='Проверка сотрудником')
    def review(self, obj):
        if obj.role != 'assistant':
            return '—'
        feedback = getattr(obj, 'feedback', None)
        if feedback:
            return format_html('<a href="{}">{} · {}</a>',
                reverse('admin:api_aimessagefeedback_change', args=[feedback.pk]),
                feedback.get_verdict_display(), feedback.reviewed_by or '—')
        return format_html('<a href="{}?message={}">Проверить ответ</a>',
            reverse('admin:api_aimessagefeedback_add'), obj.pk)


class ReviewFilter(admin.SimpleListFilter):
    title = 'Проверка'
    parameter_name = 'review'

    def lookups(self, request, model_admin):
        return [('reviewed', 'Проверен'), ('unreviewed', 'Не проверен'),
                ('correct', 'Есть правильный ответ'), ('incorrect', 'Есть неправильный ответ')]

    def queryset(self, request, queryset):
        value = self.value()
        if value == 'reviewed':
            return queryset.filter(answer_count__gt=0, unreviewed_count=0)
        if value == 'unreviewed':
            return queryset.filter(Q(unreviewed_count__gt=0) | Q(answer_count=0))
        if value in ('correct', 'incorrect'):
            return queryset.filter(messages__feedback__verdict=value).distinct()
        return queryset


@admin.register(AIConversation)
class AIConversationAdmin(admin.ModelAdmin):
    list_display = ('id', 'client', 'user', 'created_at', 'message_count', 'last_question', 'review_status')
    list_filter = ('created_at', ('client', admin.RelatedOnlyFieldListFilter), ReviewFilter)
    search_fields = ('client__company_name', 'client__inn', 'user__username', 'user__first_name',
                     'user__last_name', 'user__email', 'messages__content')
    readonly_fields = ('user', 'client', 'created_at', 'updated_at')
    inlines = (MessageInline,)
    date_hierarchy = 'created_at'
    list_select_related = ('user', 'client')

    def get_queryset(self, request):
        last = AIMessage.objects.filter(conversation=OuterRef('pk'), role='user').order_by('-pk').values('content')[:1]
        return super().get_queryset(request).annotate(
            total_messages=Count('messages', distinct=True),
            answer_count=Count('messages', filter=Q(messages__role='assistant'), distinct=True),
            unreviewed_count=Count('messages', filter=Q(messages__role='assistant') & (
                Q(messages__feedback__isnull=True) | Q(messages__feedback__verdict='unreviewed')), distinct=True),
            latest_question=Subquery(last))

    @admin.display(description='Сообщений', ordering='total_messages')
    def message_count(self, obj):
        return obj.total_messages

    @admin.display(description='Последний вопрос')
    def last_question(self, obj):
        return (obj.latest_question or '')[:160]

    @admin.display(description='Проверка')
    def review_status(self, obj):
        return 'Проверен' if obj.answer_count and not obj.unreviewed_count else 'Не проверен'

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AIMessageFeedback)
class AIMessageFeedbackAdmin(admin.ModelAdmin):
    list_display = ('id', 'message', 'verdict', 'reviewed_by', 'reviewed_at', 'approved_for_knowledge')
    list_filter = ('verdict', 'approved_for_knowledge', 'reviewed_at')
    search_fields = ('message__content', 'message__in_reply_to__content', 'corrected_answer', 'comment')
    readonly_fields = ('question', 'original_answer', 'materials', 'reviewed_by', 'reviewed_at', 'knowledge_card')
    fields = ('message', 'question', 'original_answer', 'materials', 'verdict', 'corrected_answer', 'comment',
              'approved_for_knowledge', 'knowledge_card', 'reviewed_by', 'reviewed_at')
    autocomplete_fields = ('message',)

    def get_readonly_fields(self, request, obj=None):
        return (*self.readonly_fields, 'message') if obj else self.readonly_fields

    @admin.display(description='Исходный вопрос')
    def question(self, obj):
        return obj.message.in_reply_to.content if obj and obj.message_id and obj.message.in_reply_to_id else '—'

    @admin.display(description='Оригинальный ответ ИИ (не изменяется)')
    def original_answer(self, obj):
        return obj.message.content if obj and obj.message_id else '—'

    @admin.display(description='Источники')
    def materials(self, obj):
        return source_details(obj.message) if obj and obj.message_id else '—'

    def save_model(self, request, obj, form, change):
        save_feedback(obj, request.user)

    def has_delete_permission(self, request, obj=None):
        # Revocation is explicit: uncheck publication, preserving the review record.
        return False


@admin.register(AIMessage)
class AIMessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'conversation', 'role', 'created_at', 'status')
    search_fields = ('content', 'conversation__user__username', 'conversation__client__company_name')
    list_filter = ('role', 'status', 'created_at')
    readonly_fields = tuple(field.name for field in AIMessage._meta.fields) + ('materials',)

    @admin.display(description='Источники')
    def materials(self, obj):
        return source_details(obj)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(KnowledgeDriveFile)
class KnowledgeDriveFileAdmin(admin.ModelAdmin):
    list_display = ('id', 'filename', 'status', 'active', 'last_synced_at', 'chunk_count', 'error', 'original')
    list_filter = ('status', 'active', 'last_synced_at', 'mime_type')
    search_fields = ('filename', 'file_id', 'error')
    readonly_fields = tuple(field.name for field in KnowledgeDriveFile._meta.fields) + ('chunk_count', 'original')

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(total_chunks=Count('document__chunks'))

    @admin.display(description='Фрагментов')
    def chunk_count(self, obj):
        return obj.total_chunks

    @admin.display(description='Google Drive')
    def original(self, obj):
        return format_html('<a href="{}" rel="noopener noreferrer">Открыть оригинал</a>', obj.drive_url)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(KnowledgeDocument)
class KnowledgeDocumentAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'source_type', 'status', 'active', 'created_at')
    list_filter = ('source_type', 'status', 'active')
    search_fields = ('title', 'original_name')
    readonly_fields = tuple(field.name for field in KnowledgeDocument._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
