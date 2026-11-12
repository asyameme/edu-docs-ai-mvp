"""Возможные соответствия кодов компетенций, проверенные по источникам."""

COMPETENCY_CODE_ALIASES = {
    "УНК-03": "ПКП-УНК-03",
}


def suggest_competency_code(code: str | None) -> str | None:
    """Предложить альтернативный код без изменения исходного значения."""
    if not isinstance(code, str) or not code.strip():
        return None

    code = code.strip()

    if code in COMPETENCY_CODE_ALIASES:
        return COMPETENCY_CODE_ALIASES[code]

    for alias, canonical in COMPETENCY_CODE_ALIASES.items():
        if code == canonical:
            return alias

    return None
