"""Technical-sales retrieval, evidence guards and catalog isolation regressions."""
import json
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from .models import (AIMessage, AuthToken, ClientProfile, ClientPriceOverride,
    Distributor, KnowledgeCard, KnowledgeChunk, KnowledgeDocument, Product, Region)
from .services.ai_query import detect_intent, normalize_product_identifiers, plan_query
from .services.ai_consultant import retrieve_consultation
from .services.knowledge_retrieval import grounded_answer, retrieve
from .services.product_retrieval import catalog_access, retrieve_products, select_products


@override_settings(HF_API_TOKEN='', SECURE_SSL_REDIRECT=False, ELASTICSEARCH_URL='')
class ConsultantTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user('supplier')
        cls.owner.profile.role = 'distributor'
        cls.owner.profile.save()
        cls.distributor = Distributor.objects.create(name='Дист', inn='1112223334', user=cls.owner)
        cls.region = Region.objects.create(code='77', name='Москва', distributor=cls.distributor)
        cls.user = User.objects.create_user('buyer')
        cls.client_profile = ClientProfile.objects.create(user=cls.user, inn='5556667778',
            company_name='СТО', city='Москва', contact_name='Иван', phone='+79001112233',
            region=cls.region, distributor=cls.distributor, status='active')
        AuthToken.objects.create(user=cls.user, key='buyer-token')
        cls.products = {}
        data = [
            ('EU-620', 'EU-620 2K Epoxy Primer EUMATCH', 'Грунтовки', 'Эпоксидный грунт для антикоррозионной защиты голого металла.'),
            ('EU-520', 'EU-520 Грунт наполнитель', 'Грунтовки', 'Грунт для наполнения и выравнивания поверхности.'),
            ('EU-650', 'EU-650 Грунт наполнитель', 'Грунтовки', 'Грунт для наполнения и выравнивания поверхности.'),
            ('EU-120', 'EU-120 Быстрый лак Clear Coat', 'Лаки', 'Быстрый лак для ремонтной окраски.'),
            ('EU-130', 'EU-130 Лак Clear Coat', 'Лаки', 'Прозрачный лак для ремонтной окраски.'),
            ('PL-10', 'PL-10 Грунт для пластика', 'Грунтовки', 'Адгезионный грунт для пластиковых деталей.'),
            ('DG-10', 'DG-10 Обезжириватель', 'Растворители', 'Для подготовки поверхности перед окраской. Удаляет жировые загрязнения.'),
            ('AB-10', 'AB-10 Абразив', 'Абразивы', 'Для подготовки поверхности шлифованием.'),
            ('BC-10', 'BC-10 Базовое покрытие Base Coat', 'Краски базовые', 'Базовое покрытие для ремонтной окраски.'),
            ('EU-240', 'EU-240 Hardener', 'Отвердители', 'Отвердитель для эпоксидного грунта EU-620.'),
            ('EU-310', 'EU-310 Thinner', 'Растворители', 'Разбавитель для эпоксидной системы EU-620.'),
        ]
        for sku, name, category, description in data:
            cls.products[sku] = Product.objects.create(distributor=cls.distributor, sku=sku, name=name,
                category=category, description=description, brand='EUMATCH', price='1000', quantity=17)
        cls.docs = {}
        technical = {
            'EU-620': 'EU-620 — эпоксидный грунт для антикоррозионной защиты голого металла. Смешивание EU-620 + EU-240 + EU-310 в пропорции 4:1:1. Сушка 30 минут при 20 °C.',
            'EU-520': 'EU-520 — грунт наполнитель для выравнивания поверхности. Сушка 40 минут при 20 °C.',
            'EU-650': 'EU-650 — грунт наполнитель для выравнивания поверхности. Сушка 25 минут при 20 °C.',
            'EU-120': 'EU-120 — быстрый лак EUMATCH. Применение: финишный слой после базового покрытия BC-10.',
            'EU-130': 'EU-130 — лак EUMATCH для ремонтной окраски.',
            'PL-10': 'PL-10 — грунт для пластика. Для перекраски пластикового бампера: подготовка AB-10, обезжиривание DG-10, грунт PL-10, базовое покрытие BC-10, лак EU-120. Последовательность допускается для указанной системы.',
            'DG-10': 'DG-10 — обезжириватель для подготовки поверхности. Нанести перед грунтованием.',
        }
        for sku, content in technical.items():
            cls.docs[sku] = cls.document(f'{cls.products[sku].name}_EUMATCH.pdf', content)
        cls.overview = cls.document('EUMATCH_COATS.pdf', 'EUMATCH: грунты, лаки, базовые покрытия, система смешивания. Технический обзор EUMATCH.')
        cls.html = cls.document('EUMATCH.html', 'EUMATCH ' * 200, file_type='html', chunks=12)

    @classmethod
    def document(cls, title, text, file_type='pdf', chunks=1):
        document = KnowledgeDocument.objects.create(title=title, original_name=title,
            storage_name=title, file_type=file_type, size=100, status='ready')
        KnowledgeChunk.objects.bulk_create([KnowledgeChunk(document=document, ordinal=i,
            text=text, section='Технические данные') for i in range(chunks)])
        return document

    def contexts(self, question):
        return retrieve(question, user=self.user)

    def catalog(self, contexts):
        return [c['source']['sku'] for c in contexts if c['source']['type'] == 'product']

    def test_01_brand_has_diverse_sources_not_six_html_chunks(self):
        contexts = self.contexts('Что можешь рассказать про EUMATCH?')
        docs = [c['source']['id'] for c in contexts if c['source']['type'] == 'document']
        self.assertGreaterEqual(len(set(docs)), 4)
        self.assertLessEqual(docs.count(str(self.html.pk)), 1)
        self.assertIn(str(self.overview.pk), docs)

    def test_02_lacquers_are_real_catalog_positions(self):
        contexts = self.contexts('Какие лаки EUMATCH есть?')
        self.assertEqual(set(self.catalog(contexts)), {'EU-120', 'EU-130'})
        for source in (c['source'] for c in contexts if c['source']['type'] == 'product'):
            self.assertEqual(source['sourceType'], 'product')
            self.assertTrue({'productId', 'title', 'sku', 'category', 'brand'} <= source.keys())

    def test_03_product_is_linked_to_technical_pdf_for_code_variants(self):
        for question in ('Что такое EU-620?', 'Что такое EU620?', 'Что такое eu 620?'):
            with self.subTest(question=question):
                contexts = self.contexts(question)
                self.assertIn('EU-620', self.catalog(contexts))
                pdf = next(c for c in contexts if c['source'].get('id') == str(self.docs['EU-620'].pk) and c['source']['type'] == 'document')
                self.assertIn(str(self.products['EU-620'].pk), pdf['source']['relatedProductIds'])
                product = next(c for c in contexts if c['source']['type'] == 'product')
                self.assertIn(f'document:{self.docs["EU-620"].pk}', product['source']['technicalSourceIds'])

    def test_04_mixing_only_from_tds_and_fabricated_ratio_is_rejected(self):
        contexts = self.contexts('Как смешивать EU-620?')
        self.assertTrue(any('4:1:1' in c['text'] for c in contexts if c['source']['type'] == 'document'))
        with self.settings(HF_API_TOKEN='fake'):
            with self.provider('Смешивайте EU-620 в пропорции 9:1 [1].'):
                outcome = grounded_answer('Как смешивать EU-620?', contexts)
        self.assertEqual(outcome.error, 'ungrounded_response')
        self.assertNotIn('9:1', outcome.answer)
        self.assertIn('4:1:1', outcome.answer)

    def test_05_plastic_primer_is_selected_by_purpose(self):
        self.assertEqual(self.catalog(self.contexts('Мне нужен грунт для пластика')), ['PL-10'])

    def test_06_comparison_covers_both_products_and_documents(self):
        contexts = self.contexts('Чем EU-520 отличается от EU-650?')
        self.assertEqual(set(self.catalog(contexts)), {'EU-520', 'EU-650'})
        doc_ids = {c['source']['id'] for c in contexts if c['source']['type'] == 'document'}
        self.assertTrue({str(self.docs[c].pk) for c in ('EU-520', 'EU-650')} <= doc_ids)

    def test_07_analog_does_not_promote_random_same_category_material(self):
        contexts = self.contexts('Есть аналог EU-620?')
        self.assertEqual(self.catalog(contexts), ['EU-620'])
        with self.settings(HF_API_TOKEN='fake'):
            with self.provider('Замените EU-620 на EU-777 [1].'):
                outcome = grounded_answer('Есть аналог EU-620?', contexts)
        self.assertEqual(outcome.error, 'ungrounded_response')
        self.assertNotIn('EU-777', outcome.answer)

    def test_08_unknown_sku_is_not_fuzzily_replaced(self):
        with patch('api.services.knowledge_retrieval.urllib.request.urlopen') as provider:
            contexts = self.contexts('EU-999')
            outcome = grounded_answer('EU-999', contexts)
        provider.assert_not_called()
        self.assertEqual(contexts, [])
        self.assertIn('не найден', outcome.answer)
        self.assertTrue(outcome.insufficient)

    def test_09_surface_preparation_uses_catalog(self):
        skus = self.catalog(self.contexts('Какие товары есть для подготовки поверхности?'))
        self.assertIn('DG-10', skus)
        self.assertIn('AB-10', skus)

    def test_workflow_has_distinct_stages_and_real_components(self):
        contexts = self.contexts('Мне нужно перекрасить пластиковый бампер. Что взять?')
        self.assertTrue({'PL-10', 'DG-10', 'AB-10', 'BC-10', 'EU-120'} <= set(self.catalog(contexts)))
        self.assertLessEqual(len(self.catalog(contexts)), 6)
        self.assertNotIn('EU-240', self.catalog(contexts))
        self.assertLessEqual(sum(len(c['text']) for c in contexts), 12000)

    def test_typo_english_synonyms_and_description_search(self):
        for question, sku in [('лаки еуматч', 'EU-120'), ('эпоксидник', 'EU-620'),
                              ('обезжирка', 'DG-10'), ('обезжиреватель', 'DG-10'),
                              ('Мне нужен быстрый clear coat', 'EU-120')]:
            with self.subTest(question=question):
                self.assertIn(sku, self.catalog(self.contexts(question)))
        # Purpose exists only in description, not in product name/category.
        self.assertIn('DG-10', self.catalog(self.contexts('Удаление жировых загрязнений')))

    def test_identifier_normalization_does_not_invent_code_from_russian_noun(self):
        self.assertEqual(normalize_product_identifiers('EU620 EU-620 eu 620'), ('eu620',))
        self.assertEqual(normalize_product_identifiers('эпоксидный грунт 620'), ())

    def test_title_filename_section_search_even_when_chunk_does_not_repeat_code(self):
        doc = self.document('TDS EU-777 EUMATCH.pdf', 'Только технические параметры, название не повторяется.')
        contexts = self.contexts('Что такое EU777?')
        self.assertTrue(any(c['source']['type'] == 'document' and c['source']['id'] == str(doc.pk) for c in contexts))

    def test_only_active_ready_documents_and_approved_cards_are_retrieved(self):
        KnowledgeDocument.objects.filter(pk=self.docs['EU-620'].pk).update(active=False)
        KnowledgeCard.objects.create(title='EU-620', problem='EU-620', solution='SECRET-DRAFT', status='draft')
        approved = KnowledgeCard.objects.create(title='EU-620', problem='EU-620', solution='Подтверждённая технология EU-620', status='approved', skus=['EU-620'])
        contexts = self.contexts('Как смешивать EU-620?')
        self.assertFalse(any(c['source']['type'] == 'document' and c['source']['id'] == str(self.docs['EU-620'].pk) for c in contexts))
        self.assertTrue(any(c['source']['type'] == 'card' and c['source']['id'] == str(approved.pk) for c in contexts))
        self.assertNotIn('SECRET-DRAFT', json.dumps(contexts))

    def test_customer_price_matches_catalog_and_never_other_distributor(self):
        ClientPriceOverride.objects.create(client=self.client_profile, product=self.products['EU-620'], price='735')
        foreign = Distributor.objects.create(name='Чужой', inn='0001112223')
        Product.objects.create(distributor=foreign, name='PRIVATE-SUPPLIER EU-620', sku='EU-620',
            category='Грунтовки', brand='EUMATCH', price='1', quantity=987654)
        contexts = self.contexts('Что такое EU-620?')
        product = next(c for c in contexts if c['source']['type'] == 'product')
        facts = json.loads(product['text'])
        self.assertEqual(facts['price'], 735)
        self.assertEqual(facts['quantity'], 17)
        self.assertNotIn('PRIVATE-SUPPLIER', json.dumps(contexts))
        self.assertNotIn('987654', json.dumps(contexts))
        self.assertNotIn('personalDiscount', product['text'])

    def test_catalog_roles_follow_existing_permissions(self):
        for role in ('manager', 'ai_expert', 'courier'):
            user = User.objects.create_user(role)
            user.profile.role = role
            user.profile.save()
            self.assertFalse(catalog_access(user).queryset.exists())
            self.assertEqual(self.catalog(retrieve('EU-620', user=user)), [])
        self.assertTrue(catalog_access(self.owner).queryset.exists())
        admin = User.objects.create_superuser('admin', 'a@example.com', 'pw')
        self.assertTrue(catalog_access(admin).queryset.exists())

    def test_blocked_clients_cannot_access_ai_or_catalog(self):
        self.client_profile.status = 'blocked'
        self.client_profile.save(update_fields=['status'])
        self.user.refresh_from_db()
        self.assertFalse(catalog_access(self.user).queryset.exists())
        response = self.client.post('/api/ai/chat/', {'message': 'EU-620'}, content_type='application/json', HTTP_AUTHORIZATION='Bearer buyer-token')
        self.assertEqual(response.status_code, 403)

    def test_sources_and_linked_evidence_are_saved_in_existing_history(self):
        response = self.client.post('/api/ai/chat/', {'message': 'Что такое EU-620?'}, content_type='application/json', HTTP_AUTHORIZATION='Bearer buyer-token')
        self.assertEqual(response.status_code, 200)
        self.assertTrue({'answer', 'sources', 'conversationId', 'messageId', 'suggestEscalation'} <= response.json().keys())
        reply = AIMessage.objects.get(pk=response.json()['messageId'])
        product = next(s for s in reply.sources if s['type'] == 'product')
        self.assertEqual(product['productId'], str(self.products['EU-620'].pk))
        self.assertIn('excerpt', product)
        self.assertTrue(product['technicalSourceIds'])

    def test_invalid_citations_and_fabricated_parameters_rejected(self):
        contexts = self.contexts('Как смешивать EU-620?')
        for answer in ('Сушка 987 минут [1].', 'Грунт EU-1234 [1].', 'Смешайте 4:1:1 [99].'):
            with self.subTest(answer=answer), self.settings(HF_API_TOKEN='fake'), self.provider(answer):
                outcome = grounded_answer('Как смешивать EU-620?', contexts)
                self.assertEqual(outcome.error, 'ungrounded_response')

    def test_confirmed_tds_answer_accepted_and_catalog_is_not_a_technical_citation(self):
        contexts = self.contexts('Как смешивать EU-620?')
        tds_index = next(i for i, c in enumerate(contexts, 1) if c['source']['type'] == 'document' and c['source']['id'] == str(self.docs['EU-620'].pk))
        answer = f'Смешивание EU-620 + EU-240 + EU-310: 4:1:1. Сушка 30 минут при 20 °C [{tds_index}].'
        with self.settings(HF_API_TOKEN='fake'), self.provider(answer):
            self.assertEqual(grounded_answer('Как смешивать EU-620?', contexts).status, 'completed')
        with self.settings(HF_API_TOKEN='fake'), self.provider('Сушка 30 минут [1].'):
            self.assertEqual(grounded_answer('Как смешивать EU-620?', contexts).error, 'ungrounded_response')
        self.assertTrue({'EU-240', 'EU-310'} <= set(self.catalog(contexts)))

    def test_price_and_stock_cannot_be_invented_by_model(self):
        contexts = self.contexts('Что такое EU-620?')
        for answer in ('Цена EU-620 — 7 рублей [1].', 'В наличии 987 шт. EU-620 [1].'):
            with self.subTest(answer=answer), self.settings(HF_API_TOKEN='fake'), self.provider(answer):
                self.assertEqual(grounded_answer('Что такое EU-620?', contexts).error, 'ungrounded_response')
        with self.settings(HF_API_TOKEN='fake'), self.provider('Цена EU-620 — 1000 рублей, в наличии 17 шт. [1].'):
            self.assertEqual(grounded_answer('Что такое EU-620?', contexts).status, 'completed')

    def test_unrelated_brand_materials_do_not_make_unknown_sku_known(self):
        contexts = self.contexts('Что такое EUMATCH EU-999?')
        with patch('api.services.knowledge_retrieval.urllib.request.urlopen') as call:
            outcome = grounded_answer('Что такое EUMATCH EU-999?', contexts)
        self.assertIn('не найден', outcome.answer)
        self.assertEqual(outcome.status, 'no_context')
        call.assert_not_called()

    def test_verified_alternative_requires_more_than_category(self):
        contexts = self.contexts('Чем можно заменить EU-520?')
        self.assertIn('EU-650', self.catalog(contexts))
        self.assertNotIn('EU-620', self.catalog(contexts))

    def test_followup_resolves_sku_without_replaying_assistant_claims(self):
        auth = {'HTTP_AUTHORIZATION': 'Bearer buyer-token'}
        first = self.client.post('/api/ai/chat/', {'message': 'Что такое EU-620?'}, content_type='application/json', **auth).json()
        second = self.client.post('/api/ai/chat/', {'message': 'Как его смешивать?', 'conversationId': first['conversationId']}, content_type='application/json', **auth)
        self.assertEqual(second.status_code, 200)
        self.assertIn('EU-620', [s['sku'] for s in second.json()['sources'] if s['type'] == 'product'])
        self.assertIn('4:1:1', second.json()['answer'])
        self.assertEqual(AIMessage.objects.get(pk=second.json()['messageId']).in_reply_to.content, 'Как его смешивать?')
        third = self.client.post('/api/ai/chat/', {'message': 'Есть аналог?', 'conversationId': first['conversationId']}, content_type='application/json', **auth)
        self.assertIn('EU-620', [s['sku'] for s in third.json()['sources'] if s['type'] == 'product'])

    def test_context_is_bounded_and_catalog_json_not_cut_even_with_long_descriptions(self):
        Product.objects.filter(pk=self.products['EU-620'].pk).update(description='Подробное описание. ' * 2000)
        contexts = retrieve_consultation('Какие товары EUMATCH есть?', self.user, budget=5000)
        self.assertLessEqual(sum(len(c['text']) for c in contexts), 5000)
        for item in contexts:
            if item['source']['type'] == 'product':
                self.assertIn('price', json.loads(item['text']))
            if item['source']['type'] != 'product' and 'relatedProductIds' in item['source']:
                self.assertTrue(set(item['source']['relatedProductIds']) <= {c['source']['productId'] for c in contexts if c['source']['type'] == 'product'})

    def test_large_catalog_does_not_flood_workflow_or_skip_other_stages(self):
        Product.objects.bulk_create([Product(distributor=self.distributor, sku=f'ZZ-{i}',
            name=f'Грунт EUMATCH {i}', brand='EUMATCH', category='Грунтовки', price=100) for i in range(100, 160)])
        contexts = self.contexts('Перекрасить пластиковый бампер: что взять?')
        skus = self.catalog(contexts)
        self.assertLessEqual(len(skus), 6)
        self.assertTrue({'PL-10', 'DG-10', 'BC-10', 'EU-120'} <= set(skus))

    def test_extended_domain_synonyms_work_without_code_changes(self):
        with self.settings(AI_DOMAIN_SYNONYMS={'degreaser': ['обезжириватель', 'degreaser', 'силиконремувер']}):
            self.assertIn('DG-10', self.catalog(self.contexts('силиконремувер')))

    def test_partial_sku_finds_real_articles_without_claiming_exact_identity(self):
        contexts = self.contexts('EU12')
        self.assertIn('EU-120', self.catalog(contexts))
        self.assertTrue(any(c['source'].get('queryMatch') == 'partial_sku' for c in contexts))
        self.assertIn('EU-120', self.catalog(self.contexts('EU-1')))

    def test_catalog_candidates_do_not_make_per_sku_network_calls(self):
        with self.settings(ELASTICSEARCH_URL='https://example.invalid'), patch('api.services.search._request') as external:
            self.assertIn('EU-620', self.catalog(self.contexts('Как смешивать EU-620?')))
        external.assert_not_called()

    def test_retrieval_queries_remain_bounded(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        user = User.objects.select_related('profile', 'client_profile__distributor').get(pk=self.user.pk)
        with CaptureQueriesContext(connection) as queries:
            contexts = retrieve('Мне нужно перекрасить пластиковый бампер. Что взять?', user=user)
        self.assertLessEqual(len(queries), 20)
        self.assertLessEqual(len(self.catalog(contexts)), 6)

    def test_retrieval_does_not_mutate_catalog_or_documents(self):
        before = list(Product.objects.order_by('pk').values('pk', 'price', 'quantity', 'description', 'search_text'))
        self.contexts('Мне нужен грунт для пластика')
        self.contexts('Чем заменить EU-620?')
        self.assertEqual(list(Product.objects.order_by('pk').values('pk', 'price', 'quantity', 'description', 'search_text')), before)

    def test_html_remains_usable_when_it_is_the_only_source(self):
        KnowledgeDocument.objects.exclude(pk=self.html.pk).update(active=False)
        contexts = retrieve('Что можешь рассказать про EUMATCH?')
        self.assertEqual([c['source']['id'] for c in contexts], [str(self.html.pk)])

    def test_application_finds_relevant_late_chunk_in_large_document(self):
        doc = self.document('Late EU-777 EUMATCH.pdf', 'EUMATCH, общее описание.', chunks=10)
        KnowledgeChunk.objects.create(document=doc, ordinal=10,
            text='EU-777: Смешивание в пропорции 7:1. Технические данные.', section='Смешивание')
        contexts = retrieve('Как смешивать EU-777?')
        self.assertTrue(any('7:1' in c['text'] for c in contexts))
        self.assertLessEqual(sum(c['source']['id'] == str(doc.pk) for c in contexts), 2)

    def provider(self, answer, insufficient=False):
        provider = MagicMock()
        provider.__enter__.return_value.read.return_value = json.dumps({'choices': [{'message': {
            'content': json.dumps({'answer': answer, 'insufficient': insufficient})}}]}).encode()
        return patch('api.services.knowledge_retrieval.urllib.request.urlopen', return_value=provider)


class IntentTests(TestCase):
    def test_all_required_intents(self):
        cases = {'Расскажи про EUMATCH': 'general_brand', 'Что такое EU-620?': 'product_lookup',
            'Какие у вас есть лаки?': 'assortment', 'Какой грунт мне выбрать на голый металл?': 'product_selection',
            'Почему отслаивается краска?': 'technical_problem', 'Как смешивать EU-620?': 'application',
            'Можно ли наносить этот лак на эту базу?': 'compatibility', 'Есть аналог этого грунта?': 'alternatives',
            'Чем EU-520 отличается от EU-650?': 'comparison', 'Что использовать от подготовки металла до лака?': 'workflow'}
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(detect_intent(question), expected)


@override_settings(HF_API_TOKEN='', ELASTICSEARCH_URL='')
class AssortmentFamilyTests(TestCase):
    question = 'Какие лаки EUMATCH есть в ассортименте и какой для чего подходит?'
    models = ('EU-100', 'EU-120', 'EU-130', 'EU-160', 'EU-180')

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('assortment-supplier')
        cls.user.profile.role = 'distributor'
        cls.user.profile.save()
        cls.distributor = Distributor.objects.create(name='EUMATCH supplier', inn='1234567890', user=cls.user)
        for code in cls.models:
            for suffix, size in (('', 1), ('/4', 4)):
                Product.objects.create(distributor=cls.distributor, sku=code + suffix,
                    name=f'Лак EUMATCH {code} {size}л', brand='EUMATCH', category='Лаки',
                    description='Лак для ремонтной окраски.', volume=size, price=1000, quantity=10)

    def test_focused_assortment_retrieves_beyond_four_category_rows(self):
        plan = plan_query(self.question)
        self.assertEqual(plan.intent, 'assortment')
        self.assertIn('clearcoat', plan.groups)
        candidates = retrieve_products(plan, catalog_access(self.user))
        self.assertEqual(len(candidates), 10)
        selected = select_products(candidates, plan)
        self.assertEqual([p.sku for p in selected], list(self.models))
        contexts = retrieve_consultation(self.question, self.user)
        self.assertEqual([c['source']['sku'] for c in contexts if c['source']['type'] == 'product'], list(self.models))

    def test_general_brand_overview_keeps_category_diversification(self):
        plan = plan_query('Что можешь рассказать про EUMATCH?')
        candidates = retrieve_products(plan, catalog_access(self.user))
        self.assertEqual(len(candidates), 4)

    def test_pack_variants_remain_available_for_product_lookup(self):
        plan = plan_query('Что такое EU-100?')
        candidates = retrieve_products(plan, catalog_access(self.user))
        self.assertEqual([p.sku for p in select_products(candidates, plan)], ['EU-100', 'EU-100/4'])

    def test_assortment_does_not_merge_products_without_model_codes(self):
        products = [Product(pk=i, sku=str(i), name=f'Лак {i}') for i in range(1, 4)]
        # Same category/brand alone is not a product-family identity.
        plan = plan_query('Какие лаки есть?')
        for product in products:
            product.category = 'Лаки'
        self.assertEqual(select_products(products, plan), products)
