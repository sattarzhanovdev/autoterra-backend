"""Join catalog -> TDS/cards -> related catalog; no network or writes here."""
import logging
from dataclasses import replace

from .ai_query import plan_query, normalize_product_identifiers, matched_groups, terms
from .product_retrieval import (catalog_access, product_context, product_matches_identifier,
    retrieve_products, select_products)

logger = logging.getLogger(__name__)


def retrieve_consultation(question, user, *, knowledge_limit=6, budget=12000):
    from .knowledge_retrieval import retrieve_knowledge, _related_products
    plan = plan_query(question)
    access = catalog_access(user)
    candidates = retrieve_products(plan, access)
    selected = select_products(candidates, plan)
    if plan.intent == 'alternatives' and selected:
        primary = selected[0]
        purpose_plan = replace(plan,
            search_terms=tuple(dict.fromkeys((*plan.search_terms, *terms(primary.name + ' ' + primary.description + ' ' + primary.category))))[:24],
            groups=matched_groups(primary.name + ' ' + primary.description))
        alternatives = retrieve_products(purpose_plan, access)
        candidates = list({p.pk: p for p in [primary, *alternatives]}.values())[:20]
        selected = select_products(candidates, plan)
    knowledge = retrieve_knowledge(question, limit=knowledge_limit, budget=budget,
                                   plan=plan, products=selected)
    # Technical documents may supply component/alternative codes not explicitly
    # named in the question. Retrieve them from the SAME access-scoped catalog.
    requested_products = {str(p.pk) for p in selected if any(product_matches_identifier(p, code) for code in plan.identifiers)}
    anchors = requested_products or {str(p.pk) for p in selected}
    related = tuple(dict.fromkeys(code for item in knowledge
        if not anchors or anchors.intersection(item['source'].get('relatedProductIds', []))
        for code in normalize_product_identifiers(item['source']['title'] + ' ' + item['text'])))[:12]
    if related:
        more = retrieve_products(plan, access, related_codes=related)
        candidates = list({p.pk: p for p in [*candidates, *more]}.values())
        candidates.sort(key=lambda p: -(
            2 * any(product_matches_identifier(p, code) for code in plan.identifiers)
            + any(product_matches_identifier(p, code) for code in related)))
        candidates = candidates[:20]
        # Exact requested products survive this expansion; related references
        # are candidates only, not asserted substitutes or compatible partners.
        selected = select_products(candidates, plan, related_codes=related)
    catalog = [product_context(p, access, max_chars=max(700, budget // 2 // max(1, len(selected)))) for p in selected]
    for item, product in zip(catalog, selected):
        if plan.identifiers and not any(product_matches_identifier(product, code) for code in plan.identifiers):
            item['source']['queryMatch'] = 'related' if plan.intent in ('application', 'compatibility', 'alternatives', 'workflow') else 'partial_sku'
    results = []
    # Reserve at least half of the budget for technical evidence. Descriptions
    # are bounded before pricing/stock ever reaches the model.
    for item in catalog:
        if len(item['text']) > budget:
            continue
        results.append(item)
        budget -= len(item['text'])
    for item in knowledge:
        if budget <= 0:
            break
        text = item['text'][:budget]
        results.append(dict(item, text=text))
        budget -= len(text)
    visible_products = [p for p in selected if any(
        c['source'].get('productId') == str(p.pk) for c in results)]
    for item in results:
        if item['source']['type'] != 'product':
            linked = _related_products(item['text'], item['source'], visible_products)
            if linked:
                item['source']['relatedProductIds'] = linked
            else:
                item['source'].pop('relatedProductIds', None)
    for item in results:
        if item['source']['type'] == 'product':
            item['source']['technicalSourceIds'] = list(dict.fromkeys(
                f"{other['source']['type']}:{other['source']['id']}" for other in results
                if item['source']['productId'] in other['source'].get('relatedProductIds', [])))
    logger.info('AI retrieval intent=%s products=%d sources=%d', plan.intent, len(catalog), len(knowledge))
    return results
