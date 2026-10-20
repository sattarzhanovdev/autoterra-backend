"""Bounded hybrid retrieval joining technical sources to the visible catalog."""
from collections import Counter
from dataclasses import dataclass
import json
import logging
import re
import ssl
import urllib.request

import certifi
from django.conf import settings
from django.db import connection
from django.db.models import Case, F, IntegerField, Q, Value, When, Window
from django.db.models.functions import RowNumber
from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector

from api.models import KnowledgeCard, KnowledgeChunk
from .ai_query import (STOP_WORDS, expanded_terms, matched_groups,
    normalize_product_identifiers, plan_query, terms)
from .knowledge_documents import split_text
from .product_names import normalize_search_text

logger = logging.getLogger(__name__)


def candidates(qs, fields, words, limit=60, *, diverse=False, focus_words=()):
    if not words:
        return qs.none()
    if connection.vendor == 'postgresql':
        query = SearchQuery(words[0], config='russian')
        for word in words[1:]:
            query |= SearchQuery(word, config='russian')
        vector = SearchVector(*fields, config='russian')
        qs = qs.annotate(search=vector, rank=SearchRank(vector, query)).filter(search=query)
        ordering = ['-rank', 'pk']
    else:
        query = Q(pk__in=[])
        for word in words:
            for field in fields:
                for form in dict.fromkeys((word, word.capitalize(), word.upper())):
                    query |= Q(**{f'{field}__icontains': form})
        qs = qs.filter(query)
        ordering = ['pk']
    if focus_words:
        weights = []
        for word in focus_words:
            match = Q(pk__in=[])
            for form in dict.fromkeys((word, word.capitalize(), word.upper())):
                match |= Q(text__icontains=form) | Q(section__icontains=form)
            weights.append(Case(When(match, then=Value(1)), default=Value(0), output_field=IntegerField()))
        qs = qs.annotate(_focus=sum(weights, Value(0)))
        ordering = ['-_focus', *ordering]
    if diverse:
        qs = qs.annotate(_doc_row=Window(RowNumber(), partition_by=[F('document_id')], order_by=ordering)).filter(_doc_row__lte=2)
    return qs.order_by(*ordering)[:limit]


def identifier_variants(code):
    match = re.fullmatch(r'([a-zа-я]+)(\d+)([a-z]?)', code)
    if not match:
        return [code]
    prefix, number, suffix = match.groups()
    return [code, f'{prefix}-{number}{suffix}', f'{prefix} {number}{suffix}']


def _document_source(chunk):
    doc = chunk.document
    return {'type': 'document', 'id': str(doc.pk), 'chunkId': str(chunk.pk),
            'title': doc.title, 'filename': doc.original_name, 'page': chunk.page,
            'section': chunk.section, 'sheet': chunk.sheet, 'rowStart': chunk.row_start, 'rowEnd': chunk.row_end,
            'url': doc.source_url or f'/api/knowledge-documents/{doc.pk}/download/',
            'sourceType': doc.source_type, 'fileType': doc.file_type}


def _related_products(text, source, products):
    """Link by corroborated identity, never by a shared category alone."""
    from .product_retrieval import product_identifiers
    header = ' '.join(str(source.get(key) or '') for key in ('title', 'filename', 'section'))
    codes = set(normalize_product_identifiers(header + ' ' + text))
    normalized = normalize_search_text(header + ' ' + text)
    linked = []
    for product in products:
        if codes & product_identifiers(product):
            linked.append(str(product.pk))
            continue
        name = normalize_search_text(product.name)
        if len(name) > 8 and name in normalized:
            linked.append(str(product.pk))
            continue
        # "эпоксидный грунт 620" needs independent type AND brand evidence,
        # and must identify exactly one candidate; numbers alone never link.
        for code in product_identifiers(product):
            number = re.fullmatch(r'[a-zа-я]+(\d+)[a-z]?', code)
            if not number or not re.search(r'\b' + number[1] + r'\b', header):
                continue
            if normalize_search_text(product.brand) not in normalize_search_text(header):
                continue
            if not (set(matched_groups(header)) & set(matched_groups(product.name)) - {'eumatch'}):
                continue
            peers = [p for p in products if any(c.endswith(number[1]) for c in product_identifiers(p))]
            if len(peers) == 1:
                linked.append(str(product.pk))
                break
    return list(dict.fromkeys(linked))


def retrieve_knowledge(question, limit=6, budget=8500, *, plan=None, products=()):
    plan = plan or plan_query(question)
    search_words = expanded_terms(plan)
    for product in products:
        search_words.extend([product.sku, product.name, product.brand])
    for code in plan.identifiers:
        search_words.extend(identifier_variants(code))
    intent_words = {
        'application': ['смеш', 'пропорц', 'нанес', 'сушк', 'mixing', 'ratio'],
        'compatibility': ['совмест', 'основан', 'подлож', 'совместимость', 'compatibility'],
        'alternatives': ['назначен', 'замен', 'аналог', 'применен'],
    }.get(plan.intent, [])
    search_words.extend(intent_words)
    # Bounded words, but SKU forms are retained ahead of broad expansions.
    search_words = list(dict.fromkeys([v for code in plan.identifiers for v in identifier_variants(code)] + search_words))[:60]
    if not search_words:
        return []
    qs = KnowledgeChunk.objects.filter(document__status='ready', document__active=True).select_related('document')
    chunks = {}
    # Keep indexed Russian FTS over text (including morphology), with a separate
    # title/filename/section branch. Diversity is applied BEFORE SQL LIMIT.
    for fields in (['text'], ['document__title', 'document__original_name', 'section', 'sheet']):
        for chunk in candidates(qs, fields, search_words, limit=80, diverse=True,
                                focus_words=[v for code in plan.identifiers for v in identifier_variants(code)] + intent_words):
            chunks[chunk.pk] = chunk
    ranked = []
    for chunk in chunks.values():
        source = _document_source(chunk)
        header = ' '.join((chunk.document.title, chunk.document.original_name, chunk.section, chunk.sheet))
        text = chunk.text
        normalized = normalize_search_text(header + ' ' + text)
        header_normalized = normalize_search_text(header)
        score = sum(3 if normalize_search_text(w) in header_normalized else 1
                    for w in search_words if normalize_search_text(w) in normalized)
        score += float(getattr(chunk, 'rank', 0))
        if plan.intent == 'general_brand' and re.search(r'coats|overview|обзор|ассортимент|каталог', header, re.I):
            score += 40
        codes = set(normalize_product_identifiers(header + ' ' + text))
        score += 15 * len(codes & set(plan.identifiers))
        linked = _related_products(text, source, products)
        if linked:
            source['relatedProductIds'] = linked
            score += 12
        if not score:
            continue
        priority = .35 if chunk.document.file_type.lower().lstrip('.') in ('html', 'htm') else 1
        ranked.append((score * priority, text, source))
    card_fields = ['title', 'problem', 'causes', 'solution', 'restrictions']
    # SKU JSON participates in linking and candidate discovery as well.
    cards = KnowledgeCard.objects.filter(status='approved')
    for card in candidates(cards, card_fields + ['category', 'skus'], search_words, limit=40):
        full = f'{card.title}\nКатегория: {card.category}\nSKU: {", ".join(str(s) for s in card.skus)}\nПроблема: {card.problem}\nПричины: {card.causes}\nРешение: {card.solution}\nОграничения: {card.restrictions}'
        for index, fragment in enumerate(split_text(full)):
            normalized = normalize_search_text(fragment)
            score = sum(1 for w in search_words if normalize_search_text(w) in normalized)
            score += float(getattr(card, 'rank', 0))
            if not score:
                continue
            source = {'type': 'card', 'id': str(card.pk), 'title': card.title or card.problem,
                'page': None, 'section': '', 'fragment': index,
                'url': f'/admin/api/knowledgecard/{card.pk}/change/'}
            # Only text present in this fragment establishes technical evidence.
            linked = _related_products(fragment, source, products)
            if linked:
                source['relatedProductIds'] = linked
                score += 12
            score += 15 * len(set(normalize_product_identifiers(fragment)) & set(plan.identifiers))
            ranked.append((score * 1.1, fragment, source))
    ranked.sort(key=lambda item: (-item[0], item[2]['type'], item[2]['id']))
    results, used, counts = [], set(), Counter()
    def add(item):
        nonlocal budget
        score, text, source = item
        identity = (source['type'], source['id'])
        fragment_key = (identity, source.get('chunkId', source.get('fragment')))
        if fragment_key in used or budget <= 0 or len(results) >= limit:
            return
        text = text[:min(budget, 1800)]
        results.append({'text': text, 'source': dict(source, score=round(score, 6))})
        used.add(fragment_key)
        counts[identity] += 1
        budget -= len(text)
    # First cover each requested product (comparison), then distinct sources.
    for product in (() if plan.intent == 'general_brand' else products):
        item = next((r for r in ranked if str(product.pk) in r[2].get('relatedProductIds', [])), None)
        if item:
            add(item)
    if plan.intent == 'general_brand':
        topics = set()
        for item in ranked:
            topic = tuple(g for g in matched_groups(item[2]['title']) if g != 'eumatch')
            if topic not in topics and counts[(item[2]['type'], item[2]['id'])] == 0:
                add(item)
                topics.add(topic)
    for item in ranked:
        if counts[(item[2]['type'], item[2]['id'])] == 0:
            add(item)
    if not plan.broad:
        for item in ranked:
            if counts[(item[2]['type'], item[2]['id'])] < 2:
                add(item)
    return results


def retrieve(question, limit=6, budget=8500, *, user=None):
    """Existing knowledge-only interface stays compatible; chat adds user scope."""
    if user is None:
        return retrieve_knowledge(question, limit=limit, budget=budget)
    from .ai_consultant import retrieve_consultation
    return retrieve_consultation(question, user, knowledge_limit=limit, budget=min(12000, budget + 3500))


@dataclass
class AnswerResult:
    answer: str
    insufficient: bool
    status: str = 'completed'
    error: str = ''
    attempted: bool = False

    def __iter__(self):
        # Keep existing two-value unpacking compatible.
        return iter((self.answer, self.insufficient))


def grounded_answer(question, contexts, *, history=()):
    """No context means no LLM call. Provider failure yields actual excerpts."""
    from .ai_answer_policy import SYSTEM_PROMPT, evidence_fallback, response_is_grounded
    plan = plan_query(question)
    available_codes = set(code for item in contexts for code in normalize_product_identifiers(
        item['text'] + ' ' + item['source'].get('title', '') + ' ' + item['source'].get('filename', '')))
    missing_lookup = (plan.intent in ('product_lookup', 'application') and plan.identifiers
        and not set(plan.identifiers).intersection(available_codes)
        and not any(c['source'].get('queryMatch') == 'partial_sku' for c in contexts))
    if not contexts or missing_lookup:
        if plan.identifiers:
            return AnswerResult('В доступном ассортименте и технических материалах не найден товар с артикулом '
                + ', '.join(plan.identifiers).upper() + '. Проверьте артикул или пришлите название товара.', True, 'no_context')
        return AnswerResult('В доступном ассортименте и технической базе не нашёл подтверждённых материалов для этого запроса. '
            'Уточните название материала или задачу; при необходимости подключим эксперта.', True, 'no_context')
    if not settings.HF_API_TOKEN:
        return AnswerResult(evidence_fallback(contexts), False, 'excerpts')
    context = '\n\n'.join(f'[{i}] {json.dumps(c, ensure_ascii=False)}' for i, c in enumerate(contexts, 1))
    body = {
        'model': settings.HF_CHAT_MODEL,
        'messages': [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': json.dumps({'question': question, 'intent': plan.intent,
                'requestedIdentifiers': plan.identifiers,
                'history': list(history)[-4:], 'materials': context}, ensure_ascii=False)},
        ],
        'max_tokens': 1800, 'temperature': 0.1,
        'response_format': {'type': 'json_object'},
    }
    request = urllib.request.Request(settings.HF_CHAT_URL, data=json.dumps(body).encode(),
        headers={
            'Authorization': f'Bearer {settings.HF_API_TOKEN}',
            'Content-Type': 'application/json',
            'User-Agent': 'AutoTerra/1.0',
            'Accept': 'application/json',
        })
    try:
        with urllib.request.urlopen(request, timeout=settings.HF_CHAT_TIMEOUT, context=ssl.create_default_context(cafile=certifi.where())) as response:
            result = json.loads(response.read(256_000))
        answer = json.loads(result['choices'][0]['message']['content'])
        if not isinstance(answer.get('answer'), str) or not isinstance(answer.get('insufficient'), bool):
            raise ValueError('Invalid provider response')
        if not answer['answer'].strip() or len(answer['answer']) > 16000:
            raise ValueError('Invalid answer length')
        if not response_is_grounded(answer['answer'], contexts, question):
            logger.warning('AI provider answer rejected by evidence checks')
            return AnswerResult(evidence_fallback(contexts, prefix='Ответ модели не прошёл проверку источников. Подтверждённые материалы:'),
                True, 'excerpts', 'ungrounded_response', True)
        return AnswerResult(answer['answer'], answer['insufficient'], attempted=True)
    except Exception:
        logger.warning('AI provider request failed')
        return AnswerResult(evidence_fallback(contexts, prefix='ИИ временно недоступен. Найденные выдержки из базы знаний:'),
            False, 'provider_error', 'provider_failed', True)
