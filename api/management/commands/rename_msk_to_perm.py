"""Переименовывает всё, что связано с Москвой (МСК), в Пермь.

Проходит по всем текстовым полям моделей приложения `api` и заменяет
московские написания на пермские: и русские падежи («Москве» → «Перми»),
и латинские/сокращённые метки в логинах и кодах («client1_msk» → «client1_perm»).

Отдельно правит справочник регионов: `Region.code` — не текст, а код субъекта
(77 — Москва, 59 — Пермский край), поэтому его нельзя чинить подстановкой.

Использование:
    python manage.py rename_msk_to_perm                 # что изменится (без записи)
    python manage.py rename_msk_to_perm --apply         # записать
    python manage.py rename_msk_to_perm --apply --include-vin

По умолчанию VIN не трогаем: это идентификатор автомобиля, а не адрес, и в
боевой базе там могут быть настоящие номера. В сидовых данных встречается
«VIN123MSK» — для них есть флаг --include-vin.

Прогон без --apply ничего не пишет и печатает построчно каждое изменение —
перед боевым запуском стоит вычитать этот список.
"""

import re

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

# Значения по умолчанию для справочника регионов.
DEFAULT_REGION_NAME = "Пермь - Урал"
DEFAULT_REGION_CODE = "59"

# Поля, которые не трогаем ни при каких условиях: хеш пароля может случайно
# содержать «msk», а пути к файлам развалят ссылки на уже загруженные файлы.
SKIP_FIELDS = {"password"}
SKIP_TYPES = {"FileField", "ImageField"}
TEXT_TYPES = {"CharField", "TextField", "EmailField", "SlugField", "URLField"}

# Поля с VIN — переименовываем только по флагу.
VIN_FIELDS = {"vin"}

# Длинные формы заменяем подстрокой: «Москва» не встречается внутри других слов.
# Порядок важен — сначала самые длинные, иначе «Москва - Центральный» превратится
# в «Пермь - Центральный» вместо нужного названия региона.
def build_phrase_rules(region_name: str) -> list[tuple[str, str]]:
    return [
        ("Москва - Центральный", region_name),
        ("Москва и МО", "Пермь и Пермский край"),
        ("Подмосковье", "Пермский край"),
        ("Подмосковья", "Пермского края"),
        # Пермь — центр края, а не области: «Московская область» нельзя
        # переводить общим правилом «московск» → «пермск».
        ("Московская область", "Пермский край"),
        ("Московской области", "Пермского края"),
        ("Московскую область", "Пермский край"),
        ("Московской областью", "Пермским краем"),
        ("Московская обл.", "Пермский край"),
        # Падежи. «Москвой» и «Москвы» должны идти раньше «Москва»/«Москв».
        ("Москвой", "Пермью"),
        ("Москвы", "Перми"),
        ("Москве", "Перми"),
        ("Москву", "Пермь"),
        ("Москва", "Пермь"),
        ("МОСКВОЙ", "ПЕРМЬЮ"),
        ("МОСКВЫ", "ПЕРМИ"),
        ("МОСКВЕ", "ПЕРМИ"),
        ("МОСКВУ", "ПЕРМЬ"),
        ("МОСКВА", "ПЕРМЬ"),
        ("московск", "пермск"),
        ("Московск", "Пермск"),
        ("МОСКОВСК", "ПЕРМСК"),
        ("Moscow", "Perm"),
        ("moscow", "perm"),
        ("MOSCOW", "PERM"),
    ]


# Короткие метки. Их нельзя менять простой подстрокой: «мск» лежит внутри слов
# «Томск» и «Омск». Поэтому требуем, чтобы слева и справа не было буквы —
# цифру и подчёркивание разрешаем, чтобы поймать «client1_msk» и «VIN123MSK».
LETTER = r"[^\W\d_]"
TOKEN_RULES = [
    ("MSK", "PERM"),
    ("Msk", "Perm"),
    ("msk", "perm"),
    ("МСК", "ПРМ"),
    ("Мск", "Прм"),
    ("мск", "прм"),
]


def compile_token_rules() -> list[tuple[re.Pattern, str]]:
    return [
        (re.compile(rf"(?<!{LETTER}){re.escape(src)}(?!{LETTER})"), dst)
        for src, dst in TOKEN_RULES
    ]


class Renamer:
    def __init__(self, region_name: str):
        self.phrases = build_phrase_rules(region_name)
        self.tokens = compile_token_rules()

    def apply(self, value: str) -> str:
        for src, dst in self.phrases:
            value = value.replace(src, dst)
        for pattern, dst in self.tokens:
            value = pattern.sub(dst, value)
        return value

    def walk_json(self, value):
        """Рекурсивно правит строки внутри JSONField (например Distributor.regions)."""
        if isinstance(value, str):
            return self.apply(value)
        if isinstance(value, list):
            return [self.walk_json(item) for item in value]
        if isinstance(value, dict):
            return {key: self.walk_json(item) for key, item in value.items()}
        return value


class Command(BaseCommand):
    help = "Переименовывает московские данные (МСК/Москва/msk) в пермские"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Записать изменения. Без этого флага команда только показывает найденное.",
        )
        parser.add_argument(
            "--region-name",
            default=DEFAULT_REGION_NAME,
            help=f"Новое название региона (по умолчанию «{DEFAULT_REGION_NAME}»).",
        )
        parser.add_argument(
            "--region-code",
            default=DEFAULT_REGION_CODE,
            help=f"Новый код региона (по умолчанию {DEFAULT_REGION_CODE} — Пермский край).",
        )
        parser.add_argument(
            "--include-vin",
            action="store_true",
            help="Переименовывать и VIN. По умолчанию VIN не трогаем — это номер авто, а не адрес.",
        )
        parser.add_argument(
            "--keep-logins",
            action="store_true",
            help="Не трогать логины пользователей (distributor_msk и т.п.). "
                 "По умолчанию они переименовываются — и людям придётся входить под новым логином.",
        )

    def handle(self, *args, **options):
        renamer = Renamer(options["region_name"])
        include_vin = options["include_vin"]
        include_logins = not options["keep_logins"]

        changes = self._collect(renamer, include_vin, include_logins)
        code_changes = self._collect_region_codes(options["region_code"])

        self._report(changes, code_changes, include_vin, include_logins)

        if not changes and not code_changes:
            self.stdout.write(self.style.SUCCESS("Московских данных не найдено — менять нечего."))
            return

        conflicts = self._find_conflicts([*changes, *code_changes])
        if conflicts:
            for line in conflicts:
                self.stdout.write(self.style.ERROR(f"  КОНФЛИКТ: {line}"))
            raise CommandError(
                "Переименование создаст дубликаты в уникальных полях. "
                "Разберитесь с перечисленными записями и повторите."
            )

        if not options["apply"]:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING("Пробный прогон: ничего не изменено. Повторите с флагом --apply.")
            )
            return

        written = self._write(changes, code_changes)
        self.stdout.write(self.style.SUCCESS(f"Обновлено полей: {written}"))

    # Сбор изменений

    def _target_models(self, include_logins: bool) -> list:
        """Все модели `api` плюс User: логины вида distributor_msk лежат в auth."""
        models = list(apps.get_app_config("api").get_models())
        if include_logins:
            models.append(get_user_model())
        return models

    def _collect(self, renamer: Renamer, include_vin: bool, include_logins: bool) -> list[dict]:
        changes = []
        for model in self._target_models(include_logins):
            fields = [f for f in model._meta.concrete_fields if self._is_target(f, include_vin)]
            if not fields:
                continue
            names = [f.name for f in fields]
            for obj in model.objects.only("pk", *names).iterator():
                for field in fields:
                    old = getattr(obj, field.name)
                    if old in (None, "", [], {}):
                        continue
                    new = (
                        renamer.walk_json(old)
                        if field.get_internal_type() == "JSONField"
                        else renamer.apply(old)
                    )
                    if new != old:
                        changes.append(
                            {
                                "model": model,
                                "pk": obj.pk,
                                "field": field.name,
                                "old": old,
                                "new": new,
                            }
                        )
        return changes

    def _is_target(self, field, include_vin: bool) -> bool:
        kind = field.get_internal_type()
        if kind in SKIP_TYPES or field.name in SKIP_FIELDS:
            return False
        if field.name in VIN_FIELDS and not include_vin:
            return False
        # Поля с choices — это статусы и роли, а не тексты: их менять нельзя.
        if getattr(field, "choices", None):
            return False
        return kind in TEXT_TYPES or kind == "JSONField"

    def _collect_region_codes(self, new_code: str) -> list[dict]:
        """Код региона — справочное значение, подстановкой его не починить."""
        Region = apps.get_model("api", "Region")
        moscow_codes = {"77", "97", "99", "177", "197", "199", "777", "797", "799", "50", "90", "150", "190", "750"}
        found = []
        for region in Region.objects.all():
            if region.code in moscow_codes:
                found.append({"model": Region, "pk": region.pk, "field": "code", "old": region.code, "new": new_code})
        return found

    # Проверки и запись

    def _find_conflicts(self, changes: list[dict]) -> list[str]:
        """Ищет случаи, когда новое значение займёт уже занятое уникальное поле.

        Проверяем две ситуации: новое значение уже лежит в базе, и две
        переименовываемые записи метят в одно и то же значение (в базе их
        столкновения ещё нет, а после записи будет).
        """
        problems = []
        planned: dict[tuple[str, str, str], int] = {}
        for change in changes:
            model, field_name = change["model"], change["field"]
            field = model._meta.get_field(field_name)
            if not field.unique:
                continue

            clash = model.objects.filter(**{field_name: change["new"]}).exclude(pk=change["pk"]).first()
            if clash is not None:
                problems.append(
                    f"{model.__name__}#{change['pk']}.{field_name}: «{change['new']}» "
                    f"уже занято записью #{clash.pk}"
                )
                continue

            key = (model.__name__, field_name, str(change["new"]))
            twin = planned.get(key)
            if twin is not None:
                problems.append(
                    f"{model.__name__}#{change['pk']}.{field_name}: «{change['new']}» "
                    f"получит и запись #{twin} — значение должно быть уникальным"
                )
            else:
                planned[key] = change["pk"]
        return problems

    def _write(self, changes: list[dict], code_changes: list[dict]) -> int:
        written = 0
        with transaction.atomic():
            for change in [*changes, *code_changes]:
                model = change["model"]
                # Точечный UPDATE вместо obj.save(): не дёргает сигналы и
                # auto_now, иначе переименование перепишет все даты изменения.
                model.objects.filter(pk=change["pk"]).update(**{change["field"]: change["new"]})
                written += 1
        return written

    # Вывод

    def _report(
        self,
        changes: list[dict],
        code_changes: list[dict],
        include_vin: bool,
        include_logins: bool,
    ) -> None:
        self.stdout.write(f"Найдено полей под замену: {len(changes) + len(code_changes)}")
        if not include_vin:
            self.stdout.write("  (VIN пропущен — включить флагом --include-vin)")
        self.stdout.write("")

        logins = [c for c in changes if c["field"] == "username"]
        if logins:
            self.stdout.write(
                self.style.WARNING(
                    "ВНИМАНИЕ: меняются логины — после применения эти люди войдут "
                    "только под новым логином (пароли не меняются). "
                    "Оставить как есть — флаг --keep-logins."
                )
            )
            for change in logins:
                self.stdout.write(self.style.WARNING(f"  {change['old']} → {change['new']}"))
            self.stdout.write("")
        elif not include_logins:
            self.stdout.write("  (логины пропущены — снят флаг --keep-logins, чтобы их переименовать)")
            self.stdout.write("")

        by_model: dict[str, list[dict]] = {}
        for change in [*changes, *code_changes]:
            by_model.setdefault(change["model"].__name__, []).append(change)

        for name in sorted(by_model):
            rows = by_model[name]
            self.stdout.write(self.style.MIGRATE_HEADING(f"{name} ({len(rows)})"))
            for change in rows:
                self.stdout.write(
                    f"  #{change['pk']}.{change['field']}: {change['old']!r} → {change['new']!r}"
                )
            self.stdout.write("")
