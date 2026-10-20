"""i18n manager with locale-tag normalization and safe {placeholder} formatting."""

import json
import logging
import re
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("atf.i18n")

#: A `{name}` or `{name:spec}` field in a catalog value.
PLACEHOLDER = re.compile(r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)(?:[!:][^}]*)?\}(?!\})")

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
        self._english = self._catalog.get(_FALLBACK, {})
        #: Per-language lookup tables, built once and reused.
        #:
        #: ``t()`` is the bot's hottest function — every button label, panel
        #: line and toast goes through it, several times per rendered screen —
        #: and it used to resolve the language, look the key up in the locale
        #: table, and look it up *again* in English on every single call. The
        #: merged table collapses that to one dict hit, and the resolution
        #: (`pt-BR` → `pt`, unsupported → `en`) is memoized per tag.
        self._tables: Dict[str, Dict[str, str]] = {_FALLBACK: self._english}
        self._normalized: Dict[str, str] = {}
        #: Rendered values for calls that pass placeholders, keyed by
        #: (lang, key, args). A toggle toast or a "Rule {id}" button renders
        #: the same string dozens of times per session, and ``str.format`` is
        #: the expensive part of such a call.
        self._formatted: Dict[tuple, str] = {}
        self._formatted_max = 4096

    def table(self, lang: Optional[str] = None) -> Dict[str, str]:
        """The lookup table for ``lang``, with English merged in as fallback.

        Built once per language. A locale value that is empty is *not* merged:
        the previous code read a locale hit with ``table.get(key) or
        english.get(key)``, so an empty translation already fell back to
        English, and merging it as a real value would turn that fallback into
        an empty button label.
        """
        requested = self._normalized.get(lang or "")
        if requested is None:
            requested = normalize_language(lang) or _FALLBACK
            self._normalized[lang or ""] = requested
        table = self._tables.get(requested)
        if table is None:
            table = dict(self._english)
            overlay = self._catalog.get(requested)
            if overlay:
                table.update((key, value) for key, value in overlay.items() if value)
            self._tables[requested] = table
        return table

    def set_language(self, lang: str) -> bool:
        normalized = normalize_language(lang)
        if normalized and normalized in self._catalog:
            self.current = normalized
            return True
        return False

    def t(self, key: str, lang: str = None, **kwargs) -> str:
        table = self.table(lang or self.current)
        template = table.get(key)
        if template is None:
            # A key no catalog defines renders as the key itself. That is the
            # raw-key leak a user sees in the chat, and it is silent otherwise:
            # name it, so the next one is found before a user reports it.
            logger.error("no catalog entry for key %r (lang=%s)", key, self.table_lang(lang))
            return key
        if not kwargs:
            return template
        # Key by the resolved locale, not by ``id(table)``: dict ids are
        # recycled after a rebuild, and a recycled id would hand back another
        # language's rendered string.
        cache_key = (self.table_lang(lang or self.current), key, tuple(sorted(kwargs.items())))
        cached = self._formatted.get(cache_key)
        if cached is not None:
            return cached
        try:
            rendered = template.format(**kwargs)
        except (KeyError, IndexError, ValueError) as exc:
            # Preserve a usable message if a caller has incomplete parameters —
            # but a rendered template with braces in it is still a leak, so say
            # which field the call site did not fill.
            missing = [name for name in PLACEHOLDER.findall(template) if name not in kwargs]
            logger.error(
                "translation %r (lang=%s) could not be formatted (%s); "
                "unfilled placeholder(s): %s",
                key, self.table_lang(lang), exc, ", ".join(missing) or "none",
            )
            return template
        if len(self._formatted) >= self._formatted_max:
            self._formatted.clear()
        self._formatted[cache_key] = rendered
        return rendered

    def table_lang(self, lang: Optional[str] = None) -> str:
        """The normalized locale a call resolves to (for logs and messages)."""
        return self._normalized.get(lang or "") or (normalize_language(lang) or _FALLBACK)

    def contains(self, key: str) -> bool:
        """True when the English catalog defines ``key``.

        Callers that build a key from a variable (a result code, a status) use
        this to choose a fallback *before* rendering: `t()` answers a missing
        key with the key itself, which is helpful to a developer reading a log
        and is the raw-key leak the user sees in the chat.
        """
        return key in self._english
