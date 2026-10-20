"""Read-only catalog retrieval. Every query begins with ordinary API permissions."""
from dataclasses import dataclass
from difflib import SequenceMatcher
import json

from django.db.models import Case, F, IntegerField, Q, Value, When, Window
from django.db.models.functions import RowNumber

from api.models import Product
from .ai_query import (WORKFLOW_STAGES, expanded_terms, matched_groups,
    normalize_product_identifiers, searchable_aliases)
from .product_names import compact_search_text, normalize_search_text
from .search import search_products

MAX_CANDIDATES = 20
MAX_PRODUCTS = 6


@dataclass
class CatalogAccess:
    queryset: object
    client: object = None
    commercial: bool = False


def catalog_access(user):
    """Mirror /products/ and /distributor/stock/, without granting new roles.

    Managers, experts and couriers retain knowledge access, but cannot obtain
    catalog data through AI when their ordinary catalog endpoint denies it.
    """
    from api.views import _is_user_global, _scope_products
    empty = CatalogAccess(Product.objects.none())
    if user is None or not user.is_active:
        return empty
    role = getattr(getattr(user, 'profile', None), 'role', '')
    if _is_user_global(user):
        return CatalogAccess(_scope_products(None, True).filter(is_active=True), commercial=True)
    if role == 'distributor':
        distributor = getattr(user, 'distributor_profile', None)
        if distributor and distributor.is_active:
            return CatalogAccess(_scope_products(distributor, False).filter(is_active=True), commercial=True)
    if role == 'client':
        client = getattr(user, 'client_profile', None)
        if client and client.status == 'active' and client.distributor_id and client.distributor.is_active:
            return CatalogAccess(Product.objects.filter(distributor_id=client.distributor_id, is_active=True), client, True)
    return empty


def product_identifiers(product):
    return set(normalize_product_identifiers(' '.join((product.sku, product.name)))) | {compact_search_text(product.sku)}


def product_matches_identifier(product, identifier):
    return identifier in product_identifiers(product)


def product_text(product):
    return ' '.join((product.name, product.sku, product.brand, product.category,
                     product.group_name, product.description, *product.synonyms))


def _score(product, plan):
    text = normalize_search_text(product_text(product))
    primary = normalize_search_text(' '.join((product.name, product.category, *product.synonyms)))
    groups = set(matched_groups(product_text(product)))
    score = sum(12 for code in plan.identifiers if product_matches_identifier(product, code))
    score += sum(3 for group in plan.groups if group in groups)
    score += sum(2 if normalize_search_text(term) in primary else .5 for term in plan.search_terms
                 if normalize_search_text(term) in text)
    # Fuzzy matching only ranks bounded DB candidates, never rewrites a SKU.
    tokens = primary.split()
    score += sum(.5 for term in plan.search_terms if len(term) >= 5 and any(
        SequenceMatcher(None, normalize_search_text(term), token).ratio() >= .82 for token in tokens))
    return score


def retrieve_products(plan, access, *, related_codes=(), limit=MAX_CANDIDATES):
    """Search existing normalized index, then rerank at most 20 candidates."""
    qs = access.queryset
    aliases = searchable_aliases(plan)
    if plan.intent == 'workflow':
        from .ai_query import synonym_groups
        aliases += [normalize_search_text(alias) for _, groups in WORKFLOW_STAGES
                    for group in groups for alias in synonym_groups()[group]]
    codes = tuple(dict.fromkeys((*plan.identifiers, *related_codes)))[:12]
    predicate = Q(pk__in=[])
    weights = []
    for word in dict.fromkeys(aliases):
        if len(word) < 3:
            continue
        match = Q(search_text__contains=word)
        predicate |= match
        weights.append(Case(When(match, then=Value(1)), default=Value(0), output_field=IntegerField()))
    # Reuse the catalog search's compact-SKU semantics and scope. Partial codes
    # retrieve candidates but are never treated as verified identities.
    for code in codes:
        predicate |= Q(pk__in=search_products(qs, code, rank_external=False).values('pk'))
    # A short prefix recovers spelling mistakes even with Elasticsearch off.
    if not codes:
        for term in plan.search_terms:
            normalized = normalize_search_text(term)
            if len(normalized) >= 5:
                predicate |= Q(search_text__contains=normalized[:4])
    relevance = sum(weights, Value(0))
    exact = Q(pk__in=[])
    for code in codes:
        exact |= Q(search_text__startswith=code + '\n')
    found = qs.filter(predicate).annotate(_ai_rank=relevance,
        _ai_exact=Case(When(exact, then=Value(1)), default=Value(0), output_field=IntegerField())
    )
    material_groups = {
        'primer', 'clearcoat', 'basecoat', 'hardener',
        'thinner', 'degreaser', 'abrasive',
    }
    focused_material = bool(set(plan.groups) & material_groups)
    # Diversify a general brand overview, but do not truncate a requested
    # material type (e.g. EUMATCH lacquers) to four rows in one category.
    if plan.broad and not focused_material:
        found = found.annotate(_ai_category_row=Window(RowNumber(), partition_by=[F('category')],
            order_by=['-_ai_exact', '-_ai_rank', 'pk'])).filter(_ai_category_row__lte=4)
    candidates = list(found.order_by('-_ai_exact', '-_ai_rank', 'pk')[:min(limit, MAX_CANDIDATES)])
    candidates.sort(key=lambda p: (-_score(p, plan), p.pk))
    return candidates


def primary_groups(product):
    from .product_categories import determine_product_category
    groups = set(matched_groups(product_text(product)))
    category = determine_product_category(product.name, product.category)
    mapping = {'Грунтовки': 'primer', 'Лаки': 'clearcoat', 'Краски базовые': 'basecoat',
        'Отвердители': 'hardener', 'Абразивы': 'abrasive'}
    material_types = {'primer', 'clearcoat', 'basecoat', 'hardener', 'abrasive'}
    if category in mapping:
        groups -= material_types - {mapping[category]}
        groups.add(mapping[category])
    return groups


def same_purpose(first, second):
    """Conservative alternative candidates; category alone is never enough."""
    from .ai_query import terms
    a, b = primary_groups(first), primary_groups(second)
    types = {'primer', 'clearcoat', 'basecoat', 'hardener', 'thinner', 'degreaser', 'abrasive'}
    if not (a & b & types):
        return False
    if a & b & {'epoxy', 'plastic', 'degreaser'}:
        return True
    # Exact descriptive purpose phrases may corroborate products outside the
    # small synonym dictionary. Names, codes and category do not count.
    boilerplate = {'продукт', 'материал', 'применение', 'предназначен', 'используется', 'eumatch'}
    common = (set(terms(first.description)) & set(terms(second.description))) - boilerplate
    return len(common) >= 3


def select_products(candidates, plan, limit=MAX_PRODUCTS, *, related_codes=()):
    """Reserve space for every compared SKU and for distinct workflow stages."""
    selected = []
    if not plan.identifiers and plan.intent != 'workflow':
        required = set(plan.groups) & {'primer', 'clearcoat', 'basecoat', 'hardener', 'degreaser', 'thinner', 'epoxy', 'plastic', 'eumatch'}
        if required:
            candidates = [p for p in candidates if required <= primary_groups(p)]
    def add(product):
        if product not in selected and len(selected) < limit:
            selected.append(product)
    requested = set()
    for code in plan.identifiers:
        matches = [p for p in candidates if product_matches_identifier(p, code)]
        if not matches:
            matches = [p for p in candidates if len(code) >= 4 and any(
                identifier.startswith(code) for identifier in product_identifiers(p))]
        requested.update(p.pk for p in matches)
        if matches:
            add(matches[0])
    if plan.intent == 'workflow':
        for _, groups in WORKFLOW_STAGES:
            found = [p for p in candidates if set(groups) & primary_groups(p)]
            if groups == ('hardener', 'thinner'):
                # Components belong to a system only when the selected
                # technical evidence actually references their real codes.
                found = [p for p in found if any(product_matches_identifier(p, code) for code in related_codes)]
            if found:
                add(found[0])
        return selected
    if plan.intent == 'alternatives' and selected:
        primary = selected[0]
        candidates = [p for p in candidates if p == primary or same_purpose(primary, p)]
    seen_families = set()
    for p in candidates:
        allowed = (
            (
                plan.intent in ('application', 'compatibility')
                and any(product_matches_identifier(p, code) for code in related_codes)
            )
            or not plan.identifiers
            or plan.intent in ('alternatives', 'workflow')
            or p.pk in requested
        )
        if not allowed:
            continue
        # Different pack sizes must not fill the assortment's model slots.
        if plan.intent == 'assortment':
            codes = normalize_product_identifiers(p.name)
            family = codes[0] if codes else None
            if family and family in seen_families:
                continue
            if family:
                seen_families.add(family)
        add(p)
    return selected


def product_context(product, access, *, stage='', max_chars=1800):
    # Whitelist actual model/API fields. Do not serialize model.__dict__ or
    # client discount rules, external IDs, search_text or supplier metadata.
    source = {'type': 'product', 'sourceType': 'product', 'id': str(product.pk),
              'productId': str(product.pk), 'title': product.name, 'sku': product.sku,
              'category': product.category, 'brand': product.brand}
    data = {'name': product.name, 'sku': product.sku, 'brand': product.brand, 'category': product.category,
            'groupName': product.group_name, 'description': product.description[:1200],
            'synonyms': product.synonyms[:12], 'color': product.color, 'volume': str(product.volume)}
    # No unit/subcategory/variant fields exist: groupName and volume retain
    # their real semantics. Do not invent liters or a compatibility relation.
    if access.commercial:
        from api.views import _format_product
        public = _format_product(product, access.client)
        data.update(price=public['price'], quantity=public['quantity'], status=public['status'])
    if stage:
        data['retrievalStage'] = stage
    text = json.dumps(data, ensure_ascii=False)
    # Preserve complete, valid catalog facts; shrink prose before serialization.
    while len(text) > max_chars and (data['description'] or data['synonyms']):
        data['description'] = data['description'][:max(0, len(data['description']) - 150)]
        data['synonyms'] = data['synonyms'][:-1]
        text = json.dumps(data, ensure_ascii=False)
    return {'text': text, 'source': source}
