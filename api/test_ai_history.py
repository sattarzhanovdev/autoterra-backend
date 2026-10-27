import json
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import Permission, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import AIConversation, AIMessage, AIMessageFeedback, AuthToken, ExpertTicket, KnowledgeCard
from .services.ai_history import save_feedback
from .services.knowledge_retrieval import retrieve


@override_settings(HF_API_TOKEN='')
class AIHistoryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('chat-expert', password='pw')
        self.user.profile.role = 'ai_expert'
        self.user.profile.save()
        AuthToken.objects.create(user=self.user, key='chat-token')
        self.auth = {'HTTP_AUTHORIZATION': 'Bearer chat-token'}
        self.staff = User.objects.create_superuser('reviewer', 'reviewer@example.com', 'pw')

    def chat(self, text='Primer drying', **extra):
        return self.client.post('/api/ai/chat/', json.dumps(dict(message=text, **extra)), content_type='application/json', **self.auth)

    def card(self):
        return KnowledgeCard.objects.create(problem='Primer drying', solution='Dry at 20 degrees.', status='approved')

    def feedback(self, verdict='incorrect', **kwargs):
        self.chat()
        return AIMessageFeedback(message=AIMessage.objects.get(role='assistant'), verdict=verdict, **kwargs)

    def test_exchange_saves_both_messages_and_sources_without_ticket(self):
        card = self.card()
        response = self.chat()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue({'answer', 'sources', 'sourceId', 'suggestEscalation'} <= body.keys())
        self.assertEqual(body['sourceId'], str(card.pk))
        conversation = AIConversation.objects.get()
        self.assertEqual(conversation.user, self.user)
        messages = list(conversation.messages.all())
        self.assertEqual([m.role for m in messages], ['user', 'assistant'])
        self.assertEqual(messages[0].content, 'Primer drying')
        self.assertEqual(messages[1].content, body['answer'])
        self.assertEqual(messages[1].sources[0]['id'], str(card.pk))
        self.assertIn('20 degrees', messages[1].sources[0]['excerpt'])
        self.assertEqual(messages[1].in_reply_to, messages[0])
        self.assertEqual(messages[1].status, 'excerpts')
        self.assertIsNotNone(messages[1].latency_ms)
        self.assertFalse(ExpertTicket.objects.exists())

    def test_optional_conversation_groups_messages(self):
        first = self.chat().json()
        second = self.chat('Next question', conversationId=first['conversationId']).json()
        self.assertEqual(second['conversationId'], first['conversationId'])
        self.assertEqual(AIConversation.objects.count(), 1)
        self.assertEqual(AIMessage.objects.count(), 4)

    def test_other_user_cannot_append_to_conversation(self):
        other = User.objects.create_user('other')
        conversation = AIConversation.objects.create(user=other)
        self.assertEqual(self.chat(conversationId=str(conversation.pk)).status_code, 404)
        self.assertFalse(AIMessage.objects.exists())

    def test_invalid_conversation_id(self):
        for value in ('bad', [], '9999999999999999999999999999', -2):
            with self.subTest(value=value):
                self.assertEqual(self.chat(conversationId=value).status_code, 404)

    def test_empty_and_invalid_questions_are_recorded(self):
        for text, status in [('', 200), (['bad'], 400), ('x' * 4001, 400)]:
            with self.subTest(text=str(text)[:20]):
                self.assertEqual(self.chat(text).status_code, status)
        self.assertEqual(AIMessage.objects.filter(role='user').count(), 3)
        self.assertEqual(AIMessage.objects.filter(role='assistant', status='invalid').count(), 3)

    def test_unauthorized_requests_are_not_attributed_to_other_users(self):
        self.assertEqual(self.client.post('/api/ai/chat/', {'message': 'Hi'}).status_code, 401)
        self.assertFalse(AIConversation.objects.exists())

    def test_no_context_never_calls_llm_and_is_saved(self):
        with patch('api.services.knowledge_retrieval.urllib.request.urlopen') as llm:
            self.chat()
        llm.assert_not_called()
        self.assertEqual(AIMessage.objects.get(role='assistant').status, 'no_context')

    @override_settings(HF_API_TOKEN='fake')
    def test_provider_failure_preserves_question_and_fallback(self):
        self.card()
        with patch('api.services.knowledge_retrieval.urllib.request.urlopen', side_effect=TimeoutError('secret')):
            response = self.chat()
        answer = AIMessage.objects.get(role='assistant')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(answer.status, 'provider_error')
        self.assertEqual(answer.error, 'provider_failed')
        self.assertEqual(answer.provider, 'huggingface_router')
        self.assertEqual(answer.content, response.json()['answer'])
        self.assertNotIn('secret', answer.error)
        self.assertTrue(AIMessage.objects.filter(role='user').exists())

    @override_settings(HF_API_TOKEN='fake')
    def test_llm_success_records_model_and_actual_context(self):
        self.card()
        provider = MagicMock()
        provider.__enter__.return_value.read.return_value = json.dumps({'choices': [{'message': {'content': json.dumps({
            'answer': 'Dry at 20 degrees [1].', 'insufficient': False})}}]}).encode()
        with patch('api.services.knowledge_retrieval.urllib.request.urlopen', return_value=provider) as call:
            response = self.chat()
        msg = AIMessage.objects.get(role='assistant')
        self.assertEqual(msg.status, 'completed')
        self.assertEqual(msg.content, response.json()['answer'])
        self.assertTrue(msg.model)
        body = json.loads(call.call_args.args[0].data)
        self.assertIn(msg.sources[0]['excerpt'], json.loads(body['messages'][1]['content'])['materials'])

    def test_unexpected_retrieval_failure_keeps_both_messages(self):
        with patch('api.services.knowledge_retrieval.retrieve', side_effect=RuntimeError('private')):
            response = self.chat()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(AIMessage.objects.get(role='assistant').status, 'error')
        self.assertEqual(AIMessage.objects.count(), 2)
        self.assertNotIn('private', response.content.decode())

    def test_review_never_overwrites_original_and_unapproved_not_retrieved(self):
        feedback = self.feedback(corrected_answer='Zebraprimer cures in 24 hours', comment='Checked TDS')
        original = feedback.message.content
        save_feedback(feedback, self.staff)
        feedback.refresh_from_db()
        feedback.message.refresh_from_db()
        self.assertEqual(feedback.message.content, original)
        self.assertEqual(feedback.reviewed_by, self.staff)
        self.assertIsNotNone(feedback.reviewed_at)
        self.assertEqual(retrieve('Zebraprimer'), [])

    def test_approved_correction_published_once_and_revocable(self):
        feedback = self.feedback(corrected_answer='Zebraprimer cures in 24 hours', approved_for_knowledge=True)
        save_feedback(feedback, self.staff)
        original = feedback.message.content
        self.assertTrue(retrieve('Zebraprimer'))
        save_feedback(feedback, self.staff)
        self.assertEqual(KnowledgeCard.objects.count(), 1)
        self.assertEqual(feedback.message.content, original)
        feedback.approved_for_knowledge = False
        save_feedback(feedback, self.staff)
        self.assertEqual(retrieve('Zebraprimer'), [])

    def test_correct_original_can_be_published_without_correction(self):
        self.card()
        feedback = self.feedback(verdict='correct', approved_for_knowledge=True)
        save_feedback(feedback, self.staff)
        self.assertEqual(feedback.knowledge_card.solution, feedback.message.content)

    def test_validation_rejects_unreviewed_or_missing_correct_answer(self):
        feedback = self.feedback(approved_for_knowledge=True)
        with self.assertRaises(ValidationError):
            save_feedback(feedback, self.staff)
        feedback.verdict, feedback.corrected_answer = 'unreviewed', 'Some answer'
        with self.assertRaises(ValidationError):
            save_feedback(feedback, self.staff)
        self.assertFalse(KnowledgeCard.objects.exists())

    def test_user_message_cannot_be_reviewed(self):
        self.chat()
        feedback = AIMessageFeedback(message=AIMessage.objects.get(role='user'))
        with self.assertRaises(ValidationError):
            save_feedback(feedback, self.staff)

    def test_only_staff_with_permissions_can_review_and_publish(self):
        feedback = self.feedback(corrected_answer='Zebraprimer', approved_for_knowledge=True)
        with self.assertRaises(PermissionDenied):
            save_feedback(feedback, self.user)
        reviewer = User.objects.create_user('limited', is_staff=True)
        reviewer.user_permissions.add(Permission.objects.get(codename='add_aimessagefeedback'))
        feedback.approved_for_knowledge = False
        save_feedback(feedback, reviewer)
        feedback.approved_for_knowledge = True
        with self.assertRaises(PermissionDenied):
            save_feedback(feedback, reviewer)

    def test_admin_history_search_filter_and_sequential_inline(self):
        self.card()
        data = self.chat().json()
        self.chat('Second question', conversationId=data['conversationId'])
        self.client.force_login(self.staff)
        url = reverse('admin:api_aiconversation_changelist')
        response = self.client.get(url, {'q': 'Primer drying', 'review': 'unreviewed'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'chat-expert')
        response = self.client.get(reverse('admin:api_aiconversation_change', args=[data['conversationId']]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '20 degrees')
        self.assertContains(response, 'Проверить ответ')
        self.assertLess(response.content.index(b'Primer drying'), response.content.index(b'Second question'))

    def test_admin_review_form_saves_reviewer_and_keeps_answer(self):
        self.chat()
        message = AIMessage.objects.get(role='assistant')
        self.client.force_login(self.staff)
        response = self.client.post(reverse('admin:api_aimessagefeedback_add'), {
            'message': message.pk, 'verdict': 'incorrect', 'corrected_answer': 'Zebraprimer',
            'comment': 'TDS', 'approved_for_knowledge': 'on', '_save': 'Save'})
        self.assertEqual(response.status_code, 302)
        feedback = AIMessageFeedback.objects.get()
        self.assertEqual(feedback.reviewed_by, self.staff)
        self.assertTrue(retrieve('Zebraprimer'))
        message.refresh_from_db()
        self.assertNotEqual(message.content, feedback.corrected_answer)

    def test_staff_without_model_permissions_cannot_read_history(self):
        self.chat()
        limited = User.objects.create_user('no-permissions', is_staff=True)
        self.client.force_login(limited)
        self.assertEqual(self.client.get(reverse('admin:api_aiconversation_changelist')).status_code, 403)

    def test_admin_missing_message_gives_form_error(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('admin:api_aimessagefeedback_add'), {
            'verdict': 'correct', 'approved_for_knowledge': 'on', '_save': 'Save'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AIMessageFeedback.objects.exists())

    def test_review_filters_include_only_matching_conversations(self):
        self.card()
        self.chat()
        reply = AIMessage.objects.get(role='assistant')
        save_feedback(AIMessageFeedback(message=reply, verdict='correct'), self.staff)
        self.client.force_login(self.staff)
        url = reverse('admin:api_aiconversation_changelist')
        for selected, count in [('reviewed', 1), ('unreviewed', 0), ('correct', 1), ('incorrect', 0)]:
            response = self.client.get(url, {'review': selected})
            self.assertEqual(response.context['cl'].result_count, count)

    def test_non_staff_cannot_read_history(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('admin:api_aiconversation_changelist'))
        self.assertEqual(response.status_code, 302)
