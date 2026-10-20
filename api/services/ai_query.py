"""Small, deterministic query planner shared by catalog and knowledge retrieval.

Synonyms expand searches, never assert product properties or compatibility.
AI_DOMAIN_SYNONYMS may extend/override the groups without changing Python code.
"""
from dataclasses import dataclass
from difflib import SequenceMatcher
import re

from django.conf import settings

from .product_names import compact_search_text, normalize_search_text

STOP_WORDS = set(('как какой какая какие что чем для это при или мне нужно нужен нужна можно ли и в на с по из у от не '
    'the a is how to of and расскажи рассказать можешь про есть нас вас взять использовать товар товары '
    'пожалуйста хочу под выбрать подойдет такой такое этого этот эту эту еще').split())
DOMAIN_SYNONYMS = {
    'primer': ['грунт', 'грунтов', 'primer'],
    'epoxy': ['эпоксид', 'эпоксидник', 'epoxy'],
    'clearcoat': ['лак', 'лаки', 'лаков', 'clear coat', 'clearcoat'],
    'basecoat': ['база', 'базов', 'base coat', 'basecoat'],
    'hardener': ['отвердител', 'hardener'],
    'thinner': ['разбавител', 'растворител', 'thinner'],
    'degreaser': ['обезжир', 'обезжирка', 'degreaser', 'silicone remover'],
    'plastic': ['пластик', 'пластмасс', 'plastic'],
    'metal': ['металл', 'сталь', 'стальн', 'metal', 'steel'],
    'abrasive': ['абразив', 'шлифован', 'abrasive', 'sanding'],
    'fast': ['быстр', 'скорост', 'fast', 'rapid'],
    'eumatch': ['eumatch', 'еуматч', 'эуматч'],
}
# Stage names are a retrieval aid, not a recommendation of compatibility.
WORKFLOW_STAGES = (
    ('Подготовка поверхности', ('abrasive',)),
    ('Обезжиривание', ('degreaser',)),
    ('Грунтование', ('primer',)),
    ('Базовое покрытие', ('basecoat',)),
    ('Лак', ('clearcoat',)),
    ('Сопутствующие материалы', ('hardener', 'thinner')),
)
CODE_RE = re.compile(r'(?<![^\W_])([a-zаеорстухвкмн]{1,5})[\s\-–_]*(\d{2,6})([a-z]?)(?![^\W_])', re.I)


# Share one normalization between product identities and ratio scanning.
UNICODE_DASH_TRANSLATION = str.maketrans({
    '\u2010': '-',  # hyphen
    '\u2011': '-',  # non-breaking hyphen
    '\u2012': '-',  # figure dash
    '\u2013': '-',  # en dash
    '\u2014': '-',  # em dash
    '\u2212': '-',  # minus sign
})

GENERIC_IDENTIFIER_LABELS = {'sku', 'art', 'арт'}


def normalize_product_identifiers(value):
    """EU620 / EU-620 / eu 620 share identity; generic labels are not SKUs."""
    value = str(value or '').translate(UNICODE_DASH_TRANSLATION)
    result = []
    for match in CODE_RE.finditer(value):
        prefix, number, suffix = match.groups()
        if prefix.casefold() in GENERIC_IDENTIFIER_LABELS:
            continue
        result.append(compact_search_text(''.join((prefix, number, suffix))))
    return tuple(dict.fromkeys(result))


def words(value):
    return re.findall(r'[\w]+', str(value or '').casefold().replace('ё', 'е'))


def terms(value):
    return list(dict.fromkeys(w for w in words(value) if len(w) > 2 and w not in STOP_WORDS))[:24]


def synonym_groups():
    return {**DOMAIN_SYNONYMS, **getattr(settings, 'AI_DOMAIN_SYNONYMS', {})}


def matched_groups(value, *, fuzzy=False):
    text = ' '.join(words(value))
    tokens = text.split()
    found = []
    for group, aliases in synonym_groups().items():
        if any(re.search(r'(?<!\w)' + re.escape(alias.casefold()) + r'\w*', text) for alias in aliases):
            found.append(group)
        elif fuzzy and any(len(token) >= 4 and len(alias) >= 5 and ' ' not in alias
                           and SequenceMatcher(None, token, alias).ratio() >= .82
                           for token in tokens for alias in aliases):
            found.append(group)
    return tuple(found)


def detect_intent(question):
    q = ' '.join(words(question))
    if re.search(r'отлич|сравн|разниц|versus|\bvs\b', q):
        return 'comparison'
    if re.search(r'совмест|можно ли.*(?:нанос|лак|баз|грунт)|поверх|сочета', q):
        return 'compatibility'
    if re.search(r'аналог|замен|альтернатив|что еще|что ещё', q):
        return 'alternatives'
    if re.search(r'как.*(?:смеш|развод|нанос|примен)|пропорц|время суш|давлен|сколько сло', q):
        return 'application'
    if re.search(r'почему|отсла|дефект|пузыр|кратер|потек|трещин', q):
        return 'technical_problem'
    if re.search(r'последователь|этап|систем|от.*до.*лак|(?:пере)?покрасить|перекрасить|что.*понадоб|что.*взять', q):
        return 'workflow'
    if re.search(r'выбр|подобр|подойдет|нужен|нужна|нужно|для пласт|для металл|голый металл', q):
        return 'product_selection'
    if re.search(r'какие|ассортимент|в наличии|что есть', q):
        return 'assortment'
    if normalize_product_identifiers(question) or re.search(r'что такое', q):
        return 'product_lookup'
    return 'general_brand'


@dataclass(frozen=True)
class QueryPlan:
    question: str
    intent: str
    identifiers: tuple
    groups: tuple
    search_terms: tuple

    @property
    def broad(self):
        return self.intent in ('general_brand', 'assortment', 'workflow')

    @property
    def technical(self):
        return self.intent in ('application', 'compatibility', 'alternatives', 'comparison', 'workflow', 'product_selection')


def plan_query(question):
    search_terms = terms(question)
    # Also retain short partial articles such as EU-1, which normal stop-word
    # tokenization would otherwise discard. They do not establish identity.
    search_terms.extend(re.findall(r'\b[a-z]{1,5}[- ]?\d{1,6}\b', question, re.I))
    return QueryPlan(question, detect_intent(question), normalize_product_identifiers(question),
                     matched_groups(question, fuzzy=True), tuple(dict.fromkeys(search_terms))[:24])


def expanded_terms(plan):
    result = list(plan.search_terms)
    for group in plan.groups:
        result.extend(synonym_groups()[group])
    return list(dict.fromkeys(result))[:60]


def searchable_aliases(plan):
    return list(dict.fromkeys(normalize_search_text(w) for w in expanded_terms(plan) if w))
