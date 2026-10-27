"""Smalltalk bypasses evidence retrieval; fallback never exposes raw evidence."""
import json
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings

from .models import AIConversation, AIMessage, AuthToken, Distributor, Product
from .services.ai_answer_policy import clean_client_answer, evidence_fallback
from .services.ai_consultant import retrieve_consultation
from .services.ai_query import detect_intent, plan_query
from .services.knowledge_retrieval import grounded_answer


class SmalltalkIntentTests(SimpleTestCase):
    def test_complete_social_utterances(self):
        for text in ('привет', 'Здравствуйте!', 'добрый день', 'доброе утро',
                     'добрый вечер', 'спасибо', 'благодарю', 'кто ты',
                     'что ты умеешь?', 'помоги', '  ПРИВЕТ!!!  ',
                     'Привет! Что ты умеешь?', 'помоги, пожалуйста'):
            with self.subTest(text=text):
                self.assertEqual(detect_intent(text), 'smalltalk')
                self.assertEqual(plan_query(text).intent, 'smalltalk')

    def test_social_prefix_does_not_swallow_technical_question(self):
        for text in ('привет, какой грунт выбрать для алюминия?',
                     'привет, подбери грунт для алюминия',
                     'Здравствуйте, как смешивать EU-620?',
                     'спасибо, а какой лак выбрать?', 'помоги с AIR/REGMAN',
                     'что ты умеешь рассказать про EUMATCH?',
                     'привет AIR/REGMAN', 'привет, алюминий'):
            with self.subTest(text=text):
                self.assertNotEqual(detect_intent(text), 'smalltalk')
        self.assertEqual(detect_intent('Что такое AIR/REGMAN?'), 'product_lookup')
        self.assertEqual(detect_intent('Здравствуйте, как смешивать EU-620?'), 'application')


class SafeFallbackTests(SimpleTestCase):
    contexts = [
        {'text': '{"name":"Test product","sku":"TEST-001","price":100}',
         'source': {'type': 'product', 'productId': '1'}},
        {'text': 'INTERNAL RAW DOCUMENT DATA',
         'source': {'type': 'document', 'filename': 'internal.pdf', 'id': '2'}},
    ]

    def assert_safe(self, answer):
        self.assertIn('Не удалось сформировать достаточно подтверждённый ответ.', answer)
        for private in ('{"name"', '"sku"', 'TEST-001', '100', 'productId',
                        'INTERNAL', 'internal.pdf', 'Ответ модели не прошёл проверку источников'):
            self.assertNotIn(private, answer)

    def test_fallback_never_uses_context_or_metadata(self):
        for contexts in (self.contexts, self.contexts[:1], []):
            with self.subTest(contexts=contexts):
                self.assert_safe(evidence_fallback(contexts))

    @override_settings(HF_API_TOKEN='')
    def test_missing_provider_uses_safe_fallback(self):
        with patch('api.services.knowledge_retrieval.urllib.request.urlopen') as provider:
            outcome = grounded_answer('Расскажи про товар', self.contexts)
        provider.assert_not_called()
        self.assertTrue(outcome.insufficient)
        self.assert_safe(outcome.answer)

    @override_settings(HF_API_TOKEN='fake')
    def test_provider_failure_uses_safe_fallback(self):
        with patch('api.services.knowledge_retrieval.urllib.request.urlopen', side_effect=TimeoutError('secret')):
            outcome = grounded_answer('Расскажи про товар', self.contexts)
        self.assertEqual(outcome.error, 'provider_failed')
        self.assertTrue(outcome.insufficient)
        self.assert_safe(outcome.answer)

    @override_settings(HF_API_TOKEN='fake')
    def test_ungrounded_response_still_rejected_and_hidden(self):
        provider = MagicMock()
        provider.__enter__.return_value.read.return_value = json.dumps({'choices': [{'message': {
            'content': json.dumps({'answer': 'Сушка 987 минут [1].', 'insufficient': False})}}]}).encode()
        with patch('api.services.knowledge_retrieval.urllib.request.urlopen', return_value=provider):
            outcome = grounded_answer('Расскажи про товар', self.contexts)
        self.assertEqual(outcome.error, 'ungrounded_response')
        self.assertTrue(outcome.insufficient)
        self.assert_safe(outcome.answer)
        self.assertNotIn('987', outcome.answer)

    def test_cleanup_hides_citations_and_retains_useful_links(self):
        answer = ('Подробнее: [производитель](https://manufacturer.test/help). '
                  'https://help.example.test [1] 【internal citation】\n'
                  'Источники:\n[1] internal.pdf https://docs.example.test')
        cleaned = clean_client_answer(answer, [self.contexts[1]['source']])
        self.assertIn('[производитель](https://manufacturer.test/help)', cleaned)
        self.assertIn('https://help.example.test', cleaned)
        for hidden in ('[1]', 'internal', 'Источники', 'docs.example.test'):
            self.assertNotIn(hidden, cleaned)


@override_settings(HF_API_TOKEN='fake', SECURE_SSL_REDIRECT=False, ELASTICSEARCH_URL='')
class SmalltalkChatTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('smalltalk-expert')
        cls.user.profile.role = 'admin'
        cls.user.profile.save()
        AuthToken.objects.create(user=cls.user, key='smalltalk-token')

    def chat(self, message, **extra):
        return self.client.post('/api/ai/chat/', {'message': message, **extra},
                                content_type='application/json',
                                HTTP_AUTHORIZATION='Bearer smalltalk-token')

    def test_smalltalk_never_retrieves_or_calls_grounding_or_provider(self):
        with patch('api.services.knowledge_retrieval.retrieve') as retrieval, \
             patch('api.services.ai_consultant.retrieve_consultation') as consultation, \
             patch('api.services.knowledge_retrieval.retrieve_knowledge') as knowledge, \
             patch('api.services.product_retrieval.retrieve_products') as catalog, \
             patch('api.services.knowledge_retrieval.grounded_answer') as grounding, \
             patch('api.services.ai_answer_policy.response_is_grounded') as validation, \
             patch('api.services.knowledge_retrieval.urllib.request.urlopen') as provider:
            for message in ('привет', 'Здравствуйте!', 'спасибо', 'что ты умеешь?', 'помоги'):
                with self.subTest(message=message):
                    response = self.chat(message)
                    self.assertEqual(response.status_code, 200)
                    body = response.json()
                    self.assertEqual(body['sources'], [])
                    self.assertFalse(body['suggestEscalation'])
                    self.assertIn('Пожалуйста!' if message == 'спасибо' else 'AutoTerra', body['answer'])
                    reply = AIMessage.objects.get(pk=body['messageId'])
                    self.assertEqual(reply.content, body['answer'])
                    self.assertEqual(reply.in_reply_to.content, message)
                    self.assertEqual(reply.sources, [])
                    self.assertEqual(reply.status, 'completed')
                    self.assertFalse(reply.insufficient)
                    self.assertFalse(reply.provider)
                    self.assertFalse(reply.model)
                    self.assertEqual(str(reply.conversation_id), body['conversationId'])
            for call in (retrieval, consultation, knowledge, catalog, grounding, validation, provider):
                call.assert_not_called()

    def test_smalltalk_preserves_existing_conversation(self):
        first = self.chat('привет').json()
        second = self.chat('спасибо', conversationId=first['conversationId']).json()
        self.assertEqual(second['conversationId'], first['conversationId'])
        self.assertEqual(AIConversation.objects.count(), 1)
        self.assertEqual(AIMessage.objects.count(), 4)

    def test_product_and_mixed_queries_continue_through_rag(self):
        distributor = Distributor.objects.create(name='Поставщик', inn='1234567890')
        product = Product.objects.create(distributor=distributor, sku='AIR/REGMAN', name='Регулятор давления с манометром')
        provider = MagicMock()
        provider.__enter__.return_value.read.return_value = json.dumps({'choices': [{'message': {
            'content': json.dumps({'answer': 'Регулятор давления с манометром [1].', 'insufficient': False})}}]}).encode()
        for message in ('Что такое AIR/REGMAN?', 'привет, что такое AIR/REGMAN?'):
            with self.subTest(message=message), \
                 patch('api.services.ai_consultant.retrieve_consultation', wraps=retrieve_consultation) as rag, \
                 patch('api.services.knowledge_retrieval.urllib.request.urlopen', return_value=provider) as model:
                response = self.chat(message)
                self.assertEqual(response.status_code, 200)
                rag.assert_called_once()
                model.assert_called_once()
                self.assertEqual(response.json()['answer'], 'Регулятор давления с манометром.')
                self.assertEqual(AIMessage.objects.get(pk=response.json()['messageId']).status, 'completed')
                self.assertIn(str(product.pk), [s['productId'] for s in response.json()['sources'] if s['type'] == 'product'])
                self.assertNotIn('{"name"', response.json()['answer'])

    def test_smalltalk_keeps_authorization_and_conversation_ownership_checks(self):
        response = self.client.post('/api/ai/chat/', {'message': 'привет'}, content_type='application/json')
        self.assertEqual(response.status_code, 401)
        other = User.objects.create_user('other-smalltalk-user')
        conversation = AIConversation.objects.create(user=other)
        self.assertEqual(self.chat('привет', conversationId=str(conversation.pk)).status_code, 404)
        self.assertFalse(AIMessage.objects.exists())
