"""i18n manager with locale-tag normalization and safe {placeholder} formatting."""

import json
from pathlib import Path
from typing import Dict, Optional

SUPPORTED_LANGUAGES = ["en", "fa", "ru", "zh", "pt", "uz", "id", "es", "fr", "it"]
DEFAULT_LANGUAGE = "en"
_FALLBACK = DEFAULT_LANGUAGE


def normalize_language(lang: Optional[str]) -> Optional[str]:
    """Normalize Telegram locale tags (for example, pt-BR or fa_IR)."""
    if not isinstance(lang, str):
        return None
    base = lang.strip().replace("_", "-").split("-", 1)[0].lower()
    return base if base in SUPPORTED_LANGUAGES else None


class I18n:
    def __init__(self, translations_path: str = None) -> None:
        if translations_path is None:
            translations_path = str(Path(__file__).parent / "locales" / "translations.json")
        with open(translations_path, encoding="utf-8") as fh:
            catalog = json.load(fh)
        if not isinstance(catalog, dict) or not isinstance(catalog.get(_FALLBACK), dict):
            raise ValueError("Translation catalog must contain an English dictionary.")
        self._catalog: Dict[str, Dict[str, str]] = {
            lang: table for lang, table in catalog.items() if isinstance(table, dict)
        }
        self.current = DEFAULT_LANGUAGE

    def set_language(self, lang: str) -> bool:
        normalized = normalize_language(lang)
        if normalized and normalized in self._catalog:
            self.current = normalized
            return True
        return False

    def t(self, key: str, lang: str = None, **kwargs) -> str:
        requested = normalize_language(lang or self.current) or _FALLBACK
        table = self._catalog.get(requested) or self._catalog.get(_FALLBACK, {})
        english = self._catalog.get(_FALLBACK, {})
        template = table.get(key) or english.get(key, key)
        try:
            return template.format(**kwargs) if kwargs else template
        except (KeyError, IndexError, ValueError):
            # Preserve a usable message if a caller has incomplete parameters.
            return template

    def contains(self, key: str) -> bool:
        """True when the English catalog defines ``key``.

        Callers that build a key from a variable (a result code, a status) use
        this to choose a fallback *before* rendering: `t()` answers a missing
        key with the key itself, which is helpful to a developer reading a log
        and is the raw-key leak the user sees in the chat.
        """
        return key in self._catalog.get(_FALLBACK, {})
