"""Durable chat exchanges. ExpertTicket remains a separate human support workflow."""
import json
import logging
import time

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone

from api.models import AIConversation, AIMessage, AIMessageFeedback, KnowledgeCard

logger = logging.getLogger(__name__)


def record_exchange(user, conversation, message, *, expert=False):
    from .knowledge_retrieval import retrieve, grounded_answer
    from api.views import _normalize_words

    started = time.monotonic()
    content = message if isinstance(message, str) else json.dumps(message, ensure_ascii=False)
    with transaction.atomic():
        if conversation is None:
            conversation = AIConversation.objects.create(user=user, client=getattr(user, 'client_profile', None))
        question_message = AIMessage.objects.create(conversation=conversation, role='user', content=content)
        reply = AIMessage.objects.create(conversation=conversation, role='assistant', in_reply_to=question_message, status='pending')
    question = content.strip()
    contexts = []
    http_status = 200
    if not isinstance(message, str):
        result = {'detail': 'message должен быть строкой.'}
        reply.status, reply.error, http_status = 'invalid', 'invalid_message_type', 400
    elif len(question) > 4000:
        result = {'detail': 'Вопрос слишком длинный (максимум 4000 символов).'}
        reply.status, reply.error, http_status = 'invalid', 'message_too_long', 400
    elif not _normalize_words(question):
        result = {'answer': 'Пожалуйста, введите ваш вопрос.', 'suggestEscalation': False, 'sources': []}
        reply.status = 'invalid'
    else:
        try:
            # Prior user turns only resolve references; previous assistant
            # prices/claims never become fresh evidence. Retrieval is scoped anew.
            history = list(conversation.messages.filter(role='user', pk__lt=question_message.pk)
                           .order_by('-pk').values_list('content', flat=True)[:4])
            retrieval_question = question
            import re
            from .ai_query import normalize_product_identifiers, plan_query
            plan = plan_query(question)
            followup = bool(re.search(r'\b(?:его|ее|её|этот|этого|эту|них|ним|него)\b', question, re.I))
            followup = followup or (plan.intent in ('alternatives', 'application') and not plan.groups)
            if not plan.identifiers and followup:
                anchor = next((text for text in history if normalize_product_identifiers(text)), '')
                if anchor:
                    retrieval_question += '\nКонтекст вопроса: ' + anchor[:600]
            contexts = retrieve(retrieval_question, user=user)
            outcome = grounded_answer(question, contexts, history=[text[:1000] for text in reversed(history)])
            answer, insufficient = outcome
            reply.status = outcome.status
            reply.error = outcome.error
            reply.insufficient = insufficient
            if outcome.attempted:
                reply.provider, reply.model = 'huggingface_router', settings.HF_CHAT_MODEL
            result = {'answer': answer, 'sources': [item['source'] for item in contexts],
                      'suggestEscalation': insufficient and not expert}
            first_card = next((s for s in result['sources'] if s['type'] == 'card'), None)
            if first_card:
                result['sourceId'] = first_card['id']
        except Exception:
            # No exception strings: database/provider errors may contain private text.
            logger.error('AI exchange processing failed; message_id=%s', reply.pk)
            reply.status, reply.error, http_status = 'error', 'processing_failed', 503
            result = {'answer': 'ИИ временно недоступен. Попробуйте позже или обратитесь к эксперту.',
                      'sources': [], 'suggestEscalation': not expert}
    if 'answer' in result:
        from .ai_answer_policy import clean_client_answer
        result['answer'] = clean_client_answer(result['answer'], result.get('sources', []))
    reply.content = result.get('answer', result.get('detail', ''))
    reply.sources = [dict(item['source'], excerpt=item['text']) for item in contexts]
    reply.latency_ms = max(0, round((time.monotonic() - started) * 1000))
    with transaction.atomic():
        reply.save()
        AIConversation.objects.filter(pk=conversation.pk).update(updated_at=timezone.now())
    result.update(conversationId=str(conversation.pk), messageId=str(reply.pk))
    return result, http_status


@transaction.atomic
def save_feedback(feedback, reviewer):
    """Only an explicit staff publication creates approved retrieval material."""
    if not reviewer.is_active or not reviewer.is_staff:
        raise PermissionDenied
    if not reviewer.has_perm('api.change_aimessagefeedback' if feedback.pk else 'api.add_aimessagefeedback'):
        raise PermissionDenied
    # Serialise edits/publication for this answer, including the first review.
    AIMessage.objects.select_for_update().get(pk=feedback.message_id)
    if feedback.approved_for_knowledge or feedback.knowledge_card_id:
        if not reviewer.has_perm('api.add_knowledgecard') or not reviewer.has_perm('api.change_knowledgecard'):
            raise PermissionDenied('Для публикации нужны права добавления и изменения карточек базы знаний.')
    feedback.full_clean()
    feedback.reviewed_by = reviewer
    feedback.reviewed_at = timezone.now()
    if feedback.approved_for_knowledge:
        answer = feedback.corrected_answer.strip() or feedback.message.content
        question = feedback.message.in_reply_to.content
        card = feedback.knowledge_card or KnowledgeCard()
        card.title = f'Проверенный ответ ИИ #{feedback.message_id}'
        card.category = 'Проверенные ответы ИИ'
        card.problem = question[:255]
        card.causes = f'Исходный вопрос: {question}'
        card.solution = answer
        card.status = 'approved'
        card.expert_author = reviewer
        card.save()
        feedback.knowledge_card = card
    elif feedback.knowledge_card_id:
        KnowledgeCard.objects.filter(pk=feedback.knowledge_card_id).update(status='archived', updated_at=timezone.now())
    feedback.save()
    return feedback


@receiver(post_delete, sender=AIMessageFeedback)
def archive_deleted_feedback(sender, instance, **kwargs):
    if instance.knowledge_card_id:
        KnowledgeCard.objects.filter(pk=instance.knowledge_card_id).update(status='archived', updated_at=timezone.now())
