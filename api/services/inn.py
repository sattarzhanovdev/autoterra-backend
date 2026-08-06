"""Проверка ИНН по контрольной сумме.

RegexValidator на ClientProfile проверяет только длину и то, что это цифры.
Этого мало там, где ИНН вводят руками про чужую компанию: в реферальной
программе запись по выдуманному ИНН просто повиснет в списке навсегда и
никогда ни с кем не совпадёт, а клиент будет ждать бонус.

Алгоритм стандартный (приказ ФНС): контрольные цифры считаются взвешенной
суммой по модулю 11, у 10-значного ИНН одна контрольная цифра, у 12-значного —
две.
"""

# Веса заданы алгоритмом ФНС, менять их нельзя.
_WEIGHTS_10 = (2, 4, 10, 3, 5, 9, 4, 6, 8)
_WEIGHTS_12_1 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
_WEIGHTS_12_2 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)


def _checksum(digits: list[int], weights: tuple[int, ...]) -> int:
    return sum(d * w for d, w in zip(digits, weights)) % 11 % 10


def is_valid_inn(value: str) -> bool:
    """True, если строка — синтаксически корректный ИНН (10 или 12 цифр)."""
    value = (value or "").strip()
    if not value.isdigit():
        return False
    # Одни нули формально сходятся по контрольной сумме, но таким ИНН не бывает.
    if set(value) == {"0"}:
        return False

    digits = [int(ch) for ch in value]
    if len(digits) == 10:
        return _checksum(digits[:9], _WEIGHTS_10) == digits[9]
    if len(digits) == 12:
        return (
            _checksum(digits[:10], _WEIGHTS_12_1) == digits[10]
            and _checksum(digits[:11], _WEIGHTS_12_2) == digits[11]
        )
    return False
