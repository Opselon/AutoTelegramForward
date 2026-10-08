"""i18n manager with runtime language switching and {placeholder} formatting."""

import json
from pathlib import Path
from typing import Dict

SUPPORTED_LANGUAGES = ["en", "fa", "ru", "zh"]
DEFAULT_LANGUAGE = "en"

_FALLBACK = DEFAULT_LANGUAGE


class I18n:
    def __init__(self, translations_path: str = None) -> None:
        if translations_path is None:
            translations_path = str(Path(__file__).parent / "locales" / "translations.json")
        with open(translations_path, encoding="utf-8") as fh:
            self._catalog: Dict[str, Dict[str, str]] = json.load(fh)
        self.current = DEFAULT_LANGUAGE

    def set_language(self, lang: str) -> bool:
        lang = lang.lower()
        if lang in SUPPORTED_LANGUAGES and lang in self._catalog:
            self.current = lang
            return True
        return False

    def t(self, key: str, lang: str = None, **kwargs) -> str:
        lang = (lang or self.current).lower()
        table = self._catalog.get(lang) or self._catalog.get(_FALLBACK, {})
        template = table.get(key) or self._catalog.get(_FALLBACK, {}).get(key, key)
        try:
            return template.format(**kwargs) if kwargs else template
        except (KeyError, IndexError):
            return template
