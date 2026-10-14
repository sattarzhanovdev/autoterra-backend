"""Категории пользовательского каталога и классификация номенклатуры 1С.

Название товара имеет приоритет над папками. Неизвестные значения возвращают
None: вызывающий код должен предупредить пользователя, а не создавать категорию.
"""
from __future__ import annotations

import re
import unicodedata


CANONICAL_PRODUCT_CATEGORIES = (
    "Грунтовки",
    "Шпатлёвки",
    "Лаки",
    "Краски базовые",
    "Краски 2K",
    "Растворители",
    "Отвердители",
    "Антикоры",
    "Герметики",
    "Абразивы",
    "Инструменты",
    "Расходники",
)


def _normalize(value):
    return unicodedata.normalize("NFKC", value or "").casefold().replace("ё", "е")


# Specific products precede materials they are used with: a filter for paint
# is a consumable, a spray gun for primer is a tool, a hardener for lacquer
# is a hardener. Word boundaries avoid matching "лак" inside unrelated words.
_RULES = tuple(
    (category, re.compile(pattern))
    for category, pattern in (
        (
            'Абразивы',
            r"\b(?:абразив\w*|наждач\w*|шлифшкур\w*|шкурк\w*|шлифк\w*|зачистн\w*|отрезн\w*"
            r"|scotch\s*brite|скотч\s*брайт)\b|\bкруг\w*.*(?:ржавчин|шлиф|зачист|полиров)|\bдиск\w*.*(?:шлиф"
            r"|зачист|отрез)|\b(?:полировальник\w*|кордщет\w*)\b|\b(?:полировальн\w*|шлиф\w*)[.\s]+(?:круг\w*"
            r"|бумаг\w*|лист\w*|губк\w*)\b"
        ),
        (
            'Инструменты',
            r"\b(?:инструмент\w*|оборудован\w*|бокорез\w*|плоскогуб\w*|монтиров\w*|компрессометр\w*|гайковерт\w*"
            r"|рефрактометр\w*|спектрофотометр\w*|краскопульт\w*|пистолет\w*|компрессор\w*|дрел\w*|шуруповерт\w*"
            r"|отвертк\w*|ключ\w*|трещотк\w*|пассатиж\w*|кусачк\w*|съемник\w*|домкрат\w*|молот\w*|насадк\w*"
            r"|удлинител\w*|быстросъем\w*|пневмоинструмент\w*|горелк\w*|ножниц\w*|шлифмашин\w*|шлифмашинк\w*"
            r"|шлифовалк\w*|полировалк\w*|электроинструмент\w*)\b|\bголовк\w*\s+торцев\w*|\bгубцев\w*\s+инструмент"
            r"|\b(?:переходник\w*|открывашк\w*|линейк\w*)\b"
            r"|\b(?:шлифовальн\w*|полировальн\w*)\s+машин\w*"
            r"|\bмашин\w*\s+(?:шлифовальн\w*|полировальн\w*)"
        ),
        (
            'Расходники',
            r"\b(?:расходник\w*|фильтр\w*|автокрепеж\w*|крепеж\w*|болт\w*|гайк\w*|шайб\w*|клипс\w*|пистон\w*"
            r"|скотч\w*|лент\w*|салфет\w*|ветош\w*|перчат\w*|маск\w*|респиратор\w*|емкост\w*|стакан\w*|сит\w*"
            r"|пленк\w*|укрывн\w*|мешалк\w*|сопутствующ\w*|полировальн\w*\s+паст\w*)\b"
            r"|\bсредств\w*\s+индивидуальн\w*\s+защит\w*|\b(?:наклейк\w*|этикетк\w*|комбин[ез]*он\w*|банк[аи]\w*"
            r"|крышк\w*|проставк\w*|липучк\w*|маркер\w*|пипетк\w*|аппликатор\w*)\b|\bсредств\w*\s+защит\w*"
            r"|\bкарт\w*\s+тонер\w*|\bматирующ\w*\s+паст\w*"
        ),
        (
            'Отвердители',
            r"\b(?:отвердител\w*|hardeners?)\b|\bотв-л\w*"
        ),
        (
            'Растворители',
            r"\b(?:растворител\w*|разбавител\w*|обезжиривател\w*|thinners?|degreasers?)\b|\b(?:раз-л\w*"
            r"|solvents?)\b"
        ),
        (
            'Шпатлёвки',
            r"\b(?:шпатлев\w*|шпаклев\w*|putt(?:y|ies))\b"
        ),
        (
            'Грунтовки',
            r"\b(?:грунт\w*|primers?)\b"
        ),
        (
            'Антикоры',
            r"\b(?:антикор\w*|антиграв\w*|мовил\w*|мастик\w*)\b"
        ),
        (
            'Герметики',
            r"\b(?:герметик\w*|кле[йя]\w*|sealants?)\b"
        ),
        (
            'Лаки',
            r"\b(?:лак(?:и|а|ов|ом)?|лаков\w*|clear\s*coat\w*)\b"
        ),
        (
            'Краски 2K',
            r"\b(?:краск\w*|эмал\w*|пигмент\w*|паст\w*|покрыти\w*)\b.*\b(?:2\s*[kк]|2-х|двухкомпонент\w*"
            r"|акрил\w*)\b|\b(?:2\s*[kк]|2-х|двухкомпонент\w*|акрил\w*)\b.*\b(?:краск\w*|эмал\w*|пигмент\w*"
            r"|паст\w*|покрыти\w*)\b"
        ),
        (
            'Краски базовые',
            r"\bбазов\w*\b|\b(?:краск\w*|эмал\w*|пигмент\w*|паст\w*)\b.*\b1\s*[kк]\b|\b1\s*[kк]\b.*\b(?:краск\w*"
            r"|эмал\w*|пигмент\w*|паст\w*)\b|\bbase\s*coat\w*\b"
        ),
    )
)


# Rules describe product types and homogeneous folder families, never SKUs.
# Keep reusable equipment ahead of the material it is used with, and distinguish
# abrasive-coated blocks (P-grit) from reusable sanding blocks/holders.
_TYPE_RULES = tuple(
    (category, re.compile(pattern))
    for category, pattern in (
        (
            'Инструменты',
            r"\b(?:шлифок|шлифблок|ракел\w*|оправк\w*|держател\w*|адаптер\w*|шлиф[.\s]+тарелк\w*"
            r"|опорн\w*\s+тарелк\w*)\b"
        ),
        (
            'Абразивы',
            r"\b(?:абраз[.\s]+|шлиф[.\s]+(?:мат\w*|диск\w*)|зачист[.\s]*круг|лепестков\w*|фибров\w*"
            r"|щетк\w*[-\s]*(?:кра[вц]ц?овк\w*|кравцовк\w*))|\b(?:круг\w*|диск\w*|полос\w*|лист\w*"
            r"|бумаг\w*|губк\w*|шлиф[.\s]*блок\w*)\b.*(?:\b[pр]\s*\d{2,4}\b|microfine|superfine"
            r"|ultrafine|ceramic|abralon|abranet|autonet|iridium)|\bполир\w*[.\s]*(?:круг\w*|диск\w*)"
        ),
        (
            'Инструменты',
            r"\b(?:шпател\w*|кист(?:ь|и|оч\w*)|нож(?:и|а|ей)?|резак\w*|зажим\w*|клещ\w*|сверл\w*"
            r"|кувалд\w*|надфил\w*|паяльник\w*|захват\w*|присоск\w*|распылител\w*|лопатк\w*|фонар\w*"
            r"|светильник\w*|верстак\w*|столешниц\w*|тумб\w*|тележк\w*|скреб\w*|шил(?:о|а)|дырокол\w*"
            r"|кромкогиб\w*|весы|установк\w*|манометр\w*|торнадор\w*|тройник\w*|штуцер\w*|фитинг\w*"
            r"|разветвител\w*|шланг\w*|соединени\w*|гребенк\w*|шлиф\w*\s+блок\w*)\b"
            r"|\b(?:блок\w*\s+подготовк\w*\s+воздух\w*|регулятор\w*\s+давлени\w*"
            r"|ламп\w*\s+(?:для\s+)?(?:цветоподбор\w*|колорист\w*)|(?:орбитальн\w*"
            r"|пневматическ\w*)\s+машин\w*|машин\w*\s+(?:орбитальн\w*|пневматическ\w*)"
            r"|набор\w*\s+(?:для\s+)?(?:рихтов\w*|вырезк\w*|шлифован\w*)|валик\w*\s+прикаточн\w*"
            r"|стол\w*\s+(?:поворотн\w*|окрасочн\w*)|стойк\w*[-/]стол\w*)"
        ),
        (
            'Расходники',
            r"\b(?:полумаск\w*|предфильтр\w*|изолент\w*|полотенц\w*|микрофибр\w*|салф\w*|хомут\w*"
            r"|стяжк\w*|лезви\w*|трос\w*|водосгон\w*|сметк\w*|подложк\w*|полирол\w*|воск\w*|смазк\w*"
            r"|силикагел\w*|стекломат\w*|стеклоткан\w*|смол\w*|штрих[-\s]корректор\w*|пластификатор\w*"
            r"|полупродукт\w*|бутыл\w*|бутыль|бутыли|мешок|мешки|шприц\w*|уплотнител\w*|электрод\w*"
            r"|ремень|подшипник\w*|воздухоприемник\w*|клемм\w*|разъем\w*|кабель\w*|реле|кронштейн\w*|wax"
            r"|compound|cleaner)\b|\b(?:защитн\w*\s+(?:очки|щиток\w*)|очки\b|щиток\w*\s+\w*защитн\w*"
            r"|поролонов\w*\s+валик\w*|тест[-\s]*(?:карт\w*|пластин\w*)|карт\w*\s+разбел\w*"
            r"|каталог\w*\s+цвет\w*|проявочн\w*\s+покрыти\w*|спрей[-\s]проявител\w*|контрольн\w*\s+спрей"
            r"|покрыти\w*\s+противопылев\w*|легкосним\w*[.\s]*покрыти\w*|паст\w*\s+(?:\w*\s+)?полир\w*"
            r"|полир[.\s]*паст\w*|паст\w*\s+для\s+чистки|полир[.]салф\w*|чистящ\w*\s+пластилин"
            r"|очистител\w*\s+салон\w*|средств\w*\s+для\s+удалени\w*\s+насеком\w*"
            r"|керамическ\w*\s+покрыти\w*|дистиллированн\w*\s+вод\w*|палочк\w*\s+д[ля.]*\s*размеш\w*"
            r"|набор\w*\s+для\s+полировк\w*|(?:комплект|набор)\s+для\s+защит\w*|кольц\w*\s+уплотн\w*"
            r"|сетка\s+(?:ремонтн\w*|алюминиев\w*)|(?:сварочн\w*\s+)?(?:прут\w*"
            r"|профил\w*)\s+.*(?:пластик|полипропилен|полиамид)|скоб\w*\s+для\s+ремонт\w*\s+пластик\w*"
            r"|баллон\w*\s+газов\w*|газ\s+универсальн\w*|рем[.]комплект|ремонтн\w*\s+(?:комплект|масса)"
            r"|струн\w*\s+(?:витая|для)|хлорн\w*\s+желез\w*)"
        ),
        (
            'Герметики',
            r"\b(?:холодн\w*\s+сварк\w*|стержн\w*\s+клеев\w*|уплотнительн\w*\s+масс\w*|автопластилин)"
        ),
        (
            'Грунтовки',
            r"\bпраймер\w*|\bсредств\w*\s+увеличивающ\w*\s+адгези\w*"
        ),
        (
            'Антикоры',
            r"\b(?:преобразовател\w*|запечатывател\w*)\s+ржавчин\w*|\b(?:rust\s*converter|anti[-\s]*rost"
            r"|zink[-\s]*spray)|\bсало\s+пушечн\w*"
        ),
        (
            'Растворители',
            r"\b(?:антисиликон\b|silikonentferner|sra|смывк\w*|удалител\w*\s+(?:краск\w*|автоэмал\w*))"
        ),
        (
            'Расходники',
            r"\b(?:добавк\w*|ускорител\w*\s+(?:сушк\w*|высыхани\w*)|antisilicone"
            r"|elast[-\s]*o[-\s]*actif)\b"
        ),
    )
)
# Further common workshop families. They remain semantic rules rather than
# per-code exceptions: welding profiles, protective paper, matting cloth, etc.
_ADDITIONAL_TYPE_RULES = tuple(
    (category, re.compile(pattern))
    for category, pattern in (
        (
            'Абразивы',
            r"\b(?:точильн\w*\s+камн\w*|полотно\s+матирующ\w*|щет(?:к|ок)\w*.*(?:зачис|ушм)|mbx.*щет(?:к"
            r"|ок)\w*|круг\w*\s+полир[.]|водостойк\w*\s+бумаг\w*[pр]\d+)"
        ),
        (
            'Инструменты',
            r"\b(?:фрез\w*|гибк\w*\s+вал\w*|stirring\s+lid)\b"
        ),
        (
            'Расходники',
            r"\b(?:салфетк\w*|cалфетк\w*|щиток\w*|бумаг\w*\s+на\s+пол|жидкост\w*\s+для\s+детейлинг\w*"
            r"|алюминиев\w*\s+сетк\w*|палочк\w*.*размеш)"
        ),
        (
            'Краски базовые',
            r"\b(?:краск\w*\s+структурн\w*|структурн\w*\s+краск\w*|автоэмал\w*\s+металлик)"
        ),
    )
)
# Scope family rules to the nearest descriptive group (not generic "Разное",
# "001.Краски" or a mixed brand). Product-type rules always win over these.
_FOLDER_RULES = tuple(
    (category, re.compile(pattern))
    for category, pattern in (
        (
            'Инструменты',
            r"металлическая мебель|столы/стойки|рихтовочные аксессуары|пневматические машинки|(?:^"
            r"|\s)машинки$|шланги$|сварочные аксессуары"
        ),
        (
            'Расходники',
            r"шумоизоляц|виброизоляц|(?:^|\s)запчасти$|полумаск|материалы общего назначения"
            r"|смола и ткань|сварочн\w*\s+материал"
        ),
        (
            'Абразивы',
            r"(?:круги|полосы).*(?:multiholes|керамика|abralon|abranet|autonet|iridium)"
            r"|перфорированные листы.*goldflex|шлифовальн\w*\s+материал"
            r"|шлифовальн\w*\s+блоки.*(?:120х98х13|98х69х26)|водостойкая.*бумага"
        ),
        (
            'Краски 2K',
            r"sikkens\s+тонеры\s+autocoat\s+bt(?:\s+scv)?$|^u-pol\s+raptor$"
        ),
        (
            'Краски базовые',
            r"аэрозоли серии power can$|hymax\s+спецпродукты$|hymax\s+перламутровые\s+компоненты"
            r"|пигментные пасты|пигменты\s+palizh"
        ),
    )
)
# Known paint-system families, as opposed to individual catalogue codes.
# See docs/ONE_C_CATEGORY_CLASSIFICATION.md for the manufacturer references.
_TWO_COMPONENT_PAINT = re.compile(r"\b(?:hardmax\s+pur|mobihard\s+пу|autocoat\s+bt(?!\s+120)|raptor)\b")
_PAINT = re.compile(r"\b(?:краск\w*|эмал\w*|автоэмал\w*|покрыти\w*|пок[.]|пигмент\w*|паст\w*|колорстрим\w*|перламутр\w*|металлик\w*|биндер\w*)\b")
_ONE_COMPONENT_PAINT = re.compile(r"\b(?:1\s*[kк]|однокомпонент\w*|аэрозоль\w*|аэр[.]?|спрей|алкидн\w*|термостойк\w*|bumper\s+color)\b")
_TWO_COMPONENT_MARKER = re.compile(r"\b(?:2\s*[kк]|двухкомпонент\w*)\b|(?:с\s+отв[.]|\+\s*отвердител)")
_TINTING_COMPONENT = re.compile(r"\b(?:пигмент\w*|колорстрим\w*|перламутр\w*|колер\w*\s+паст\w*|паст\w*\s+колер[.]?\w*)")


def _classify_text(text):
    for category in CANONICAL_PRODUCT_CATEGORIES:
        if text.strip() == _normalize(category):
            return category
    candidates = []
    # Match the product itself before its packaging or application: a primer
    # "с кисточкой", remover "в бутылке", or rust converter "с распылителем"
    # must not become a brush, bottle or sprayer. Equal starts prefer specific
    # type rules to generic material words (e.g. polishing paste).
    patterns = (*_ADDITIONAL_TYPE_RULES, *_TYPE_RULES, *_RULES)
    for category, pattern in patterns:
        if category in ("Краски 2K", "Краски базовые"):
            continue
        match = pattern.search(text)
        if match:
            candidates.append((match.start(), -(match.end() - match.start()), category))
    material = _PAINT.search(text)
    paint_category = None
    if material:
        if _TWO_COMPONENT_MARKER.search(text):
            paint_category = "Краски 2K"
        elif _ONE_COMPONENT_PAINT.search(text):
            paint_category = "Краски базовые"
        elif _TWO_COMPONENT_PAINT.search(text):
            paint_category = "Краски 2K"
        else:
            for category, pattern in (*_ADDITIONAL_TYPE_RULES, *_RULES):
                if category in ("Краски 2K", "Краски базовые") and pattern.search(text):
                    paint_category = category
                    break
        if paint_category is None and _TINTING_COMPONENT.search(text):
            # Owner-approved catalog policy for universal tinting components.
            paint_category = "Краски базовые"
        if paint_category:
            candidates.append((material.start(), -(material.end() - material.start()), paint_category))
    return min(candidates, key=lambda item: item[:2])[2] if candidates else None


def determine_product_category(name, folder="", hierarchy=()):
    """Return one canonical category or None; hierarchy is root-to-leaf."""
    text_name = _normalize(name)
    context = [_normalize(value) for value in (folder, *hierarchy)]
    # Hydraulic cylinders belong to workshop equipment in the WIEDER KRAFT
    # family; the same bare noun in a mixed catalogue is not enough evidence.
    if re.search(r"\b(?:гидроцилиндр\w*|пневмоцилиндр\w*)", text_name):
        return "Инструменты"
    if re.search(r"\bцилиндр\w*", text_name) and any(
            re.fullmatch(r"wieder\s*kraft", value.strip()) for value in context):
        return "Инструменты"
    if re.search(r"сварочн\w*\s+материал|прут\w*.*(?:свароч|пластик|pp\d)", text_name):
        return "Расходники"
    category = _classify_text(text_name)
    if category is not None:
        return category
    if "fullen-polymer" in context and re.search(r"профил|для ремонт\w* пластик", text_name):
        return "Расходники"
    for value in (folder, *reversed(tuple(hierarchy))):
        text = _normalize(value)
        category = _classify_text(text)
        if category is not None:
            return category
        for category, pattern in _FOLDER_RULES:
            if pattern.search(text):
                return category
    return None
