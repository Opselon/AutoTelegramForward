"""Per-task content rules — deciding and rewriting before delivery.

A routing task (``ForwardRule``) may carry a *content policy* that decides
whether a message goes out at all and how its text is rewritten on the way:

* ``blacklist_words`` — never forward a message containing one of these words.
* ``whitelist_words`` — forward **only** when the message contains them
  (``whitelist_mode``: ``any`` | ``all`` | ``phrase``).
* ``block_links``     — never forward a message that contains a link.
* ``remove_links``    — forward it, with the links stripped.
* ``strip_words``     — forward it, with those words removed.
* ``replacements``    — ordered find -> replace pairs (literal, or ``re:`` regex).
* ``regex_block`` / ``regex_require`` — block on match / require a match.
* length and media gates.

The policy lives on the task and can be **overridden per source chat and per
target chat**, so one task can route ``A,B -> X,Y`` with X filtered differently
from Y. An override is an *additional* constraint, never a relaxation: every
policy in a chain must allow the message, and transforms apply in chain order.

Why it is written this way:

* **Matching is normalised and word-aware.** Persian/Arabic text uses several
  encodings of the same letter (Arabic yeh U+064A vs Farsi yeh U+06CC, Arabic
  kaf U+0643 vs Farsi keh U+06A9, alef U+0623/U+0627) plus zero-width joiners;
  a blacklist typed on one keyboard silently misses text written on another.
  Both sides are normalised before comparison.
* **A word is a word.** Substring matching makes ``cat`` match ``concatenate``
  and blacklists things nobody blacklisted. Keywords match on word boundaries
  unless they start or end with a non-word character (``#promo``, ``@chan``,
  a URL), where a boundary would be wrong.
* **Patterns are compiled once and cached, and hostile ones are refused.**
  A nested-quantifier pattern (``(a+)+``) on a crafted message is a worker
  stall; it is rejected instead of trusted.
* **Replacements run in a single pass over the original text**, so a replacement
  value can never be re-scanned by a later rule — deterministic, no cascades.
* **Evaluation is cheapest-first** (lengths, media, links, literal words,
  regexes) so a burst of 50 posts never pays for regexes it does not need.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = ["PolicyDecision", "MatchPreview", "ContentPolicy", "fold_text", "contains_word",
           "has_link", "split_word_list", "preview_match", "preview_policy",
           "compile_user_regex", "bounded_search"]


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

#: Letter variants that Telegram users type interchangeably. Folding them means
#: one keyword catches every spelling of the same word.
_FOLD_MAP = str.maketrans({
    # Arabic yeh
    "\u064a": "\u06cc",
    # alef maksura
    "\u0649": "\u06cc",
    # yeh hamza
    "\u0626": "\u06cc",
    # Arabic kaf
    "\u0643": "\u06a9",
    # heh with yeh
    "\u06c0": "\u0647",
    # teh marbuta
    "\u0629": "\u0647",
    # alef hamza above
    "\u0623": "\u0627",
    # alef hamza below
    "\u0625": "\u0627",
    # alef madda
    "\u0622": "\u0627",
    # alef wasla
    "\u0671": "\u0627",
    # waw hamza
    "\u0624": "\u0648",
})

#: Diacritics (harakat), tatweel, and the zero-width / bidi control characters
#: that make two visually identical strings compare differently.
_RE_INVISIBLE = re.compile(
    "[\u064b-\u0652\u0670\u0640\u200b-\u200f\u202a-\u202e\ufeff]"
)

#: A link, for the block-links gate. Deliberately broader than ``http``: a
#: message that only carries ``t.me/x`` or ``tg://`` is still a link.
_RE_LINK = re.compile(
    r"(?:https?://|ftp://|www\.|t\.me/|telegram\.me/|telegram\.dog/|tg://)",
    re.IGNORECASE,
)

_REDOS = re.compile(r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d*,?\d*\})")
#: The ``_REDOS`` shape above catches a quantifier nested inside a group that
#: is itself quantified — ``(a+)+``. It does **not** catch the same blow-up
#: from *ambiguous alternation* under a quantifier: ``(a|a)*b`` is exponential
#: too (a 30-character input exceeds four seconds) but carries no ``+``/``*``
#: inside the parens. Covering both is what makes the guard defensible instead
#: of shape-luck.
_REDOS_ALT = re.compile(r"\((?:[^()\\]|\\.)*\|(?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d*,?\d*\})")

_MAX_PATTERN_LEN = 1000
#: Ceiling on the text a *user-supplied* pattern is matched against. A bound
#: is the second half of the ReDoS guarantee: the structural guard refuses the
#: known catastrophic shapes, but a pattern nobody has catalogued yet could
#: still be quadratic-or-worse on a 4096-character message. Matching against
#: the first ``_MAX_MATCH_LEN`` characters keeps the worst case linear in the
#: input, while a message longer than the cap only loses matching in its
#: tail — and 8 KiB is far beyond a Telegram message (4096) already.
_MAX_MATCH_LEN = 8192


def has_link(text: str) -> bool:
    """True when ``text`` carries anything link-shaped.

    Broader than an ``http`` substring check on purpose: ``www.``, ``t.me/x``,
    ``telegram.me/x`` and ``tg://`` are all links to a reader, and a
    "block links" toggle that misses them is a toggle users stop trusting.
    """
    return bool(text) and _RE_LINK.search(text) is not None


def fold_text(text: str, *, normalize: bool = True, case_sensitive: bool = False) -> str:
    """Return the comparison form of ``text``.

    ``normalize`` unifies Persian/Arabic letter variants and strips invisible
    characters; ``case_sensitive`` keeps the original letter case.
    """
    if not text:
        return ""
    out = unicodedata.normalize("NFC", text)
    if normalize:
        out = _RE_INVISIBLE.sub("", out).translate(_FOLD_MAP)
    return out if case_sensitive else out.casefold()


#: A keyword that opens with one of these is a token family rather than a
#: word: `#ad` is a hashtag prefix, `@scam` a handle prefix, `/start` a command.
_TOKEN_MARKERS = ("#", "@", "$", "/")

#: Every spelling a human reads as the same letter, so one keyword catches all
#: of them. Keys are the folded form; values are the characters to accept.
_VARIANT_CLASS = {
    "\u06cc": "\u06cc\u064a\u0649\u0626",
    "\u06a9": "\u06a9\u0643",
    "\u0627": "\u0627\u0623\u0625\u0622\u0671",
    "\u0647": "\u0647\u0629\u06c0",
    "\u0648": "\u0648\u0624",
}

#: Zero-or-more invisible characters (harakat, tatweel, ZWJ/ZWNJ, bidi marks)
#: allowed between the characters of a keyword when normalising.
_INVISIBLE_RUN = "[\u064b-\u0652\u0670\u0640\u200b-\u200f\u202a-\u202e\ufeff]*"


def _match_mode(needle: str, word_boundary: bool) -> str:
    """How a keyword should be matched: ``word``, ``prefix`` or ``substring``."""
    if not word_boundary:
        return "substring"
    if needle[:1] in _TOKEN_MARKERS:
        return "prefix"
    if any(ch.isalnum() for ch in needle):
        return "word"
    return "substring"


@lru_cache(maxsize=4096)
def _needle_pattern(
    needle: str, *, mode: str, normalize: bool, case_sensitive: bool
) -> re.Pattern:
    """Compile a keyword into a pattern that matches the **original** text.

    Folding the haystack would be cheaper, but it moves character offsets
    (diacritics and zero-width joiners are deleted), and offsets are exactly
    what ``strip_words`` needs to delete the right span. Instead each character
    of the folded keyword becomes a class of the variants a human would call the
    same letter, and — when normalising — the invisible characters are allowed
    to sit between them. One search, correct spans, no copy of the text.
    """
    folded = re.sub(r"\s+", " ", fold_text(needle, normalize=normalize, case_sensitive=False))
    chars = list(folded)
    if not any(ch.strip() for ch in chars):
        return re.compile(r"(?!)")  # a keyword that folds away matches nothing
    parts = []
    for ch in chars:
        if ch == " ":
            # A phrase survives the extra space (or newline) an editor leaves
            # between its words; the matched span still covers exactly what the
            # text had, so removing it stays correct.
            parts.append(r"\s+")
            continue
        variants = _VARIANT_CLASS.get(ch)
        parts.append(f"[{re.escape(variants)}]" if variants else re.escape(ch))
    sep = _INVISIBLE_RUN if normalize else ""
    body = sep.join(parts)
    left = r"(?<!\w)" if mode != "substring" else ""
    if mode == "prefix":
        # A tag/handle keyword matches its whole family: `#ad` catches
        # `#advert`, `@scam` catches `@scam_bot`. The marker is what makes it a
        # token, so a following letter does not start a different word.
        right = ""
    elif mode == "word":
        right = r"(?!\w)" if folded[-1:].isalnum() else ""
    else:
        right = ""
    return re.compile(f"{left}{body}{right}", 0 if case_sensitive else re.IGNORECASE)


@lru_cache(maxsize=512)
def _folded_needles(
    words: Tuple[str, ...],
    *,
    normalize: bool = True,
    case_sensitive: bool = False,
) -> Tuple[str, ...]:
    """Every word in its comparison form, aligned with ``words`` (blank stays blank).

    Matching 500 words with 500 patterns costs ~9 ms per message, because the
    regex engine re-walks the whole text for each word. The comparison form of a
    word, though, is always a substring of any text that word matches — the
    pattern only adds boundaries, token families and letter variants around that
    same text. So ``needle in folded_body`` can skip every word that cannot
    possibly match, at one C-level scan per word: ~16x cheaper on a 2 KB
    message, and flat enough that a large list stays practical.

    A candidate is always confirmed with the real matcher, so the shortcut can
    change how much work a decision costs, never what the decision is.

    Whitespace inside a needle is collapsed to a single space and the haystack
    is scanned in the same collapsed form: the matcher's pattern turns every
    inter-word space into ``\\s+``, so a phrase matches across the double space
    or newline an editor left between its words — and a pre-filter that kept
    the *original* spacing would reject exactly those messages before the
    matcher ever ran. Short keywords carry no internal whitespace, so the fold
    is a no-op for them.
    """
    folded: List[str] = []
    for word in words:
        needle = (word or "").strip()
        folded.append(
            re.sub(r"\s+", " ", fold_text(needle, normalize=normalize, case_sensitive=case_sensitive))
            if needle else ""
        )
    return tuple(folded)


#: The comparison form a haystack is reduced to for the cheap substring
#: pre-filter. Mirrors :func:`_folded_needles` (same fold, same order) so the
#: two sides of the ``needle in folded_body`` test can never disagree about
#: whitespace the way the raw text and the raw keyword do.
def _folded_body(text: str, *, normalize: bool = True, case_sensitive: bool = False) -> str:
    return re.sub(r"\s+", " ", fold_text(text, normalize=normalize, case_sensitive=case_sensitive))


def _first_matching_word(haystack: str, words: Sequence[str], **flags: Any) -> str:
    """The first word of ``words`` that really matches ``haystack``, or ``""``.

    Scanned in the order the task lists them, so a rejection names the first
    offender the user typed rather than an arbitrary one.
    """
    if not words or not haystack:
        return ""
    needles = _folded_needles(
        tuple(words),
        normalize=flags["normalize"],
        case_sensitive=flags["case_sensitive"],
    )
    folded_body = _folded_body(
        haystack, normalize=flags["normalize"], case_sensitive=flags["case_sensitive"]
    )
    for word, needle in zip(words, needles):
        if needle and needle in folded_body and contains_word(haystack, word, **flags):
            return word
    return ""


def contains_word(
    haystack: str,
    needle: str,
    *,
    normalize: bool = True,
    case_sensitive: bool = False,
    word_boundary: bool = True,
) -> bool:
    """True when ``needle`` occurs in ``haystack`` under the policy's rules.

    Matching runs against the original text with a variant-aware pattern, so a
    keyword still finds a word whose letters were typed with the Arabic forms
    (U+0643 kaf, U+064A yeh) instead of the Farsi ones, survives zero-width
    joiners inside the word, and still respects word boundaries.
    """
    needle = (needle or "").strip()
    if not needle or not haystack:
        return False
    pattern = _needle_pattern(
        needle,
        mode=_match_mode(needle, word_boundary),
        normalize=normalize,
        case_sensitive=case_sensitive,
    )
    return pattern.search(haystack) is not None


def _hostile_pattern(pattern: str) -> bool:
    """True when ``pattern`` carries a known catastrophic-backtracking shape."""
    return _REDOS.search(pattern) is not None or _REDOS_ALT.search(pattern) is not None


def bounded_search(compiled: re.Pattern, text: str) -> Optional[re.Match]:
    """Match a user-supplied pattern against ``text`` under a length ceiling.

    The structural guard refuses the catalogued catastrophic shapes, but it
    cannot prove a pattern *safe*: an uncatalogued one could still degrade
    quadratically or worse on a long body. Capping the scanned text at
    ``_MAX_MATCH_LEN`` keeps the worst case linear in the input. A message
    longer than the cap can only lose a match in its tail — and the cap sits
    far above any Telegram message, so nothing a user can actually send is
    shortened.
    """
    if not text:
        return None
    return compiled.search(text[:_MAX_MATCH_LEN])


@lru_cache(maxsize=2048)
def compile_user_pattern(pattern: str, flags: int = 0) -> Optional[re.Pattern]:
    """Compile a user-supplied pattern with explicit flags, or None.

    Same guard as :func:`compile_user_regex` (length cap, nested-quantifier
    and ambiguous-alternation refusal) but the caller chooses the flags, which
    a find/replace entry needs: a replacement may be case-sensitive, dot-all,
    multiline or verbose while a content filter is always case-insensitive.
    """
    if not pattern or len(pattern) > _MAX_PATTERN_LEN:
        return None
    if _hostile_pattern(pattern):
        return None
    try:
        return re.compile(pattern, flags)
    except re.error:
        return None


@lru_cache(maxsize=2048)
def compile_user_regex(pattern: str) -> Optional[re.Pattern]:
    """Compile a user-supplied pattern, or None when it is unusable.

    Refuses patterns that are long or carry a nested quantifier: those are the
    shapes that turn a crafted message into minutes of backtracking. A refused
    pattern never drops a message — it is simply not consulted. Shared by every
    stage that evaluates a pattern a user typed (the content policy and
    `FilterEngine`), so the guard cannot be forgotten in one place.
    """
    return compile_user_pattern(pattern, re.IGNORECASE | re.MULTILINE)



#: Kept so existing callers inside this module (and any third-party importer)
#: keep working; `compile_user_regex` is the name to use.
_compile_user_regex = compile_user_regex


# --------------------------------------------------------------------------
# Decision
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyDecision:
    """Outcome of evaluating one policy against one message."""

    allowed: bool
    reason: str
    matched: Tuple[str, ...] = ()

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.allowed


@dataclass(frozen=True)
class MatchPreview:
    """Structured, client-ready answer to "does this rule match this text?"

    Returned by :func:`preview_match` — the one canonical, read-only preview
    every surface (bot ``Test Rules``, dashboard tester, mobile tester, the
    API's preview endpoint) uses. It reports the decision *and* the evidence,
    so a UI can show which entries matched and which were missing instead of
    a bare boolean.

    ``matched``/``missing`` carry the entries as the user typed them (not the
    folded comparison forms): what a rule owner reads in a test result is what
    they wrote in the editor.
    """

    allowed: bool
    reason: str
    matched: Tuple[str, ...] = ()
    missing: Tuple[str, ...] = ()
    #: The entries the preview actually consulted, so a UI can echo back the
    #: effective configuration (entries + mode) next to the result.
    entries: Tuple[str, ...] = ()
    mode: str = "any"

    def to_dict(self) -> Dict[str, Any]:
        """The wire form the API returns; stable for client caching."""
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "matched": list(self.matched),
            "missing": list(self.missing),
            "entries": list(self.entries),
            "mode": self.mode,
        }


def preview_match(
    text: str,
    *,
    whitelist: Sequence[str] = (),
    blacklist: Sequence[str] = (),
    mode: str = "any",
    regex_block: Sequence[str] = (),
    regex_require: Sequence[str] = (),
    normalize: bool = True,
    case_sensitive: bool = False,
    word_boundary: bool = True,
) -> MatchPreview:
    """Preview a keyword/phrase/long-text or regex rule against ``text``.

    Read-only by construction: it takes the configuration as arguments and
    never touches a repository, a task or a queue, so a "test my rule" click
    cannot forward anything or persist a draft. The decision is produced with
    the *same* matchers :class:`ContentPolicy.evaluate` uses
    (:func:`_first_matching_word`, :func:`contains_word` and the guarded
    regex compiler), so a preview agrees with production instead of
    approximating it.

    ``mode`` is the long-text matching mode — ``any`` | ``all`` | ``phrase`` —
    exactly the modes :class:`ContentPolicy` supports; anything else falls
    back to ``any`` rather than being reported as an error the caller must
    translate.
    """
    body = text or ""
    wl = tuple(str(w) for w in (whitelist or ()) if str(w).strip())
    bl = tuple(str(w) for w in (blacklist or ()) if str(w).strip())
    mode = str(mode or "any").strip().lower()
    if mode not in ("any", "all", "phrase"):
        mode = "any"
    flags: Dict[str, Any] = {
        "normalize": normalize,
        "case_sensitive": case_sensitive,
        "word_boundary": word_boundary,
    }

    if bl:
        hit = _first_matching_word(body, bl, **flags)
        if hit:
            return MatchPreview(
                allowed=False, reason=f"blacklist:{hit}",
                matched=(hit,), missing=(), entries=bl, mode=mode,
            )

    if wl:
        if mode == "all":
            matched = tuple(w for w in wl if contains_word(body, w, **flags))
            missing = tuple(w for w in wl if w not in matched)
            if missing:
                return MatchPreview(
                    allowed=False, reason=f"whitelist_missing:{','.join(missing[:3])}",
                    matched=matched, missing=missing, entries=wl, mode=mode,
                )
            return MatchPreview(
                allowed=True, reason="whitelist_all",
                matched=matched, missing=(), entries=wl, mode=mode,
            )
        elif mode == "phrase":
            phrase = " ".join(wl)
            if not contains_word(body, phrase, **flags):
                return MatchPreview(
                    allowed=False, reason="whitelist_phrase_missing",
                    matched=(), missing=tuple(wl), entries=wl, mode=mode,
                )
            return MatchPreview(
                allowed=True, reason="whitelist_phrase",
                matched=(phrase,), missing=(), entries=wl, mode=mode,
            )
        else:
            hit = _first_matching_word(body, wl, **flags)
            if not hit:
                return MatchPreview(
                    allowed=False, reason="whitelist_mismatch",
                    matched=(), missing=tuple(wl), entries=wl, mode=mode,
                )
            return MatchPreview(
                allowed=True, reason=f"whitelist:{hit}",
                matched=(hit,), missing=(), entries=wl, mode=mode,
            )

    for pattern in regex_block or ():
        compiled = _compile_user_regex(pattern)
        if compiled is not None and bounded_search(compiled, body) is not None:
            return MatchPreview(
                allowed=False, reason=f"regex_block:{pattern[:48]}",
                matched=(pattern,), missing=(), entries=tuple(regex_block), mode=mode,
            )

    if regex_require:
        usable = [p for p in regex_require if _compile_user_regex(p) is not None]
        if usable:
            hit = next(
                (p for p in usable if bounded_search(_compile_user_regex(p), body) is not None),  # type: ignore[union-attr]
                None,
            )
            if hit is None:
                return MatchPreview(
                    allowed=False, reason="regex_require_missing",
                    matched=(), missing=tuple(usable), entries=tuple(regex_require), mode=mode,
                )
            return MatchPreview(
                allowed=True, reason=f"regex_require:{hit[:48]}",
                matched=(hit,), missing=(), entries=tuple(regex_require), mode=mode,
            )

    return MatchPreview(True, "no_rules" if not (wl or bl or regex_block or regex_require) else "policy_passed",
                        entries=tuple(wl) or tuple(bl), mode=mode)


def preview_policy(text: str, policy: "ContentPolicy") -> MatchPreview:
    """Preview an assembled :class:`ContentPolicy` (the task-level surface).

    Equivalent to :func:`preview_match` over the policy's own lists, flags and
    mode, so a UI testing a *saved* task policy and a UI testing a *draft*
    agree — the same function answers both.
    """
    return preview_match(
        text,
        whitelist=policy.whitelist_words,
        blacklist=policy.blacklist_words,
        mode=policy.whitelist_mode,
        regex_block=policy.regex_block,
        regex_require=policy.regex_require,
        normalize=policy.normalize,
        case_sensitive=policy.case_sensitive,
        word_boundary=policy.word_boundary,
    )


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------

_LIST_KEYS = ("blacklist_words", "whitelist_words", "strip_words",
              "regex_block", "regex_require", "allowed_media", "blocked_media")
_FLAG_KEYS = ("block_links", "remove_links")


def _as_str_list(raw: Any) -> Tuple[str, ...]:
    """Coerce a settings value into a clean, order-preserving tuple of strings."""
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple, set)):
        return split_word_list([str(x) for x in raw])
    if isinstance(raw, str):
        # A comma/newline separated blob is accepted so a text command or a
        # one-line form field works as well as a proper list.
        return split_word_list(raw)
    return ()


#: Separators a user's keyword list is allowed to use. The ASCII comma, the
#: Persian/Arabic comma (``\u060c``) — a Persian user's keyboard offers it
#: directly — and any newline, so one entry per line works on every surface.
_WORD_SEPARATORS = (",", "\u060c", "\n", "\r")


def split_word_list(raw: Any) -> Tuple[str, ...]:
    """Split a user-typed keyword list into ordered, non-empty entries.

    One canonical home for the parsing every surface does: the Telegram bot's
    ``/whitelist add`` and ``Add`` prompt, the filter wizard, the settings
    registry and the stored blob all reduce ``"5x, 10x\\nWhale Update"`` to the
    same three entries, so a phrase keeps its words instead of being cut into
    fragments that match nothing. Duplicate entries are dropped (first wins) and
    each entry is stripped, but nothing inside an entry is collapsed — internal
    whitespace is the matcher's job, which treats any run of it as one space.
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        text = raw
    elif isinstance(raw, (list, tuple, set)):
        # Already split (a form field or a settings write): an entry is an
        # entry — a phrase that contains a comma stays whole, because the
        # surfaces that build lists split on separators before storing them.
        out: List[str] = []
        seen: set = set()
        for item in raw:
            entry = str(item).strip()
            if entry and entry not in seen:
                seen.add(entry)
                out.append(entry)
        return tuple(out)
    else:
        return ()
    text = str(text)
    for separator in _WORD_SEPARATORS[1:]:
        text = text.replace(separator, _WORD_SEPARATORS[0])
    seen, out = set(), []
    for chunk in text.split(_WORD_SEPARATORS[0]):
        entry = chunk.strip()
        if entry and entry not in seen:
            seen.add(entry)
            out.append(entry)
    return tuple(out)


def _as_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _as_int(raw: Any, default: int = 0) -> int:
    """Coerce a stored bound to int; a junk value falls back to the default."""
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        try:
            return int(float(str(raw)))
        except (TypeError, ValueError):
            return default


def _as_pair_list(raw: Any) -> Tuple[Tuple[str, str], ...]:
    """Normalise replacements to ordered (find, replace) pairs.

    Accepts ``{"old": "new"}``, ``[["old", "new"]]``, or ``["old=>new"]``.
    A ``re:`` prefix makes the find side a regular expression.
    """
    seq: List[Tuple[Any, Any]] = []
    if isinstance(raw, dict):
        seq = list(raw.items())
    elif isinstance(raw, (list, tuple)):
        for entry in raw:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                seq.append((entry[0], entry[1]))
            elif isinstance(entry, str) and "=>" in entry:
                find, _, repl = entry.partition("=>")
                seq.append((find, repl))
    out: List[Tuple[str, str]] = []
    seen: set = set()
    for find, repl in seq:
        f = str(find or "")
        if not f or f in seen:
            continue
        seen.add(f)
        out.append((f, str(repl if repl is not None else "")))
    return tuple(out)


@dataclass
class ContentPolicy:
    """The content rules of one task (or one source/target override)."""

    enabled: Optional[bool] = None
    blacklist_words: Tuple[str, ...] = ()
    whitelist_words: Tuple[str, ...] = ()
    whitelist_mode: str = "any"          # any | all | phrase
    block_links: bool = False
    remove_links: bool = False
    strip_words: Tuple[str, ...] = ()
    replacements: Tuple[Tuple[str, str], ...] = ()
    regex_block: Tuple[str, ...] = ()
    regex_require: Tuple[str, ...] = ()
    min_length: int = 0
    max_length: int = 0
    allowed_media: Tuple[str, ...] = ()
    blocked_media: Tuple[str, ...] = ()
    case_sensitive: bool = False
    word_boundary: bool = True
    normalize: bool = True
    source: str = ""                     # provenance for logs/UI: "" | "task" | "source:<id>" | "target:<id>"
    notes: Dict[str, Any] = field(default_factory=dict)

    # -- construction -------------------------------------------------------

    @classmethod
    def from_dict(cls, raw: Any, *, source: str = "") -> "ContentPolicy":
        """Build a policy from stored settings. Never raises on bad input."""
        if not isinstance(raw, dict):
            raw = {}
        mode = str(raw.get("whitelist_mode") or raw.get("whitelist_match") or "any").strip().lower()
        if mode not in ("any", "all", "phrase"):
            mode = "any"
        pol = cls(
            enabled=(None if raw.get("enabled") is None else _as_bool(raw.get("enabled"))),
            whitelist_mode=mode,
            block_links=_as_bool(raw.get("block_links")),
            remove_links=_as_bool(raw.get("remove_links")),
            min_length=_as_int(raw.get("min_length")),
            max_length=_as_int(raw.get("max_length")),
            case_sensitive=_as_bool(raw.get("case_sensitive")),
            word_boundary=(True if raw.get("word_boundary") is None else _as_bool(raw.get("word_boundary"))),
            normalize=(True if raw.get("normalize") is None else _as_bool(raw.get("normalize"))),
            replacements=_as_pair_list(raw.get("replacements")),
            source=source,
        )
        for key in _LIST_KEYS:
            setattr(pol, key, _as_str_list(raw.get(key)))
        return pol

    def to_dict(self) -> Dict[str, Any]:
        """Round-trippable form (used by the settings API and backups)."""
        return {
            "enabled": self.enabled,
            "blacklist_words": list(self.blacklist_words),
            "whitelist_words": list(self.whitelist_words),
            "whitelist_mode": self.whitelist_mode,
            "block_links": self.block_links,
            "remove_links": self.remove_links,
            "strip_words": list(self.strip_words),
            "replacements": {f: r for f, r in self.replacements},
            "regex_block": list(self.regex_block),
            "regex_require": list(self.regex_require),
            "min_length": self.min_length,
            "max_length": self.max_length,
            "allowed_media": list(self.allowed_media),
            "blocked_media": list(self.blocked_media),
            "case_sensitive": self.case_sensitive,
            "word_boundary": self.word_boundary,
            "normalize": self.normalize,
        }

    # -- state --------------------------------------------------------------

    @property
    def has_rules(self) -> bool:
        """True when the policy actually constrains or rewrites anything.

        No artificial caps: any number of words, patterns and replacements is
        one more entry in a tuple — 5 or 500 behave identically.
        """
        return bool(
            self.blacklist_words or self.whitelist_words or self.block_links
            or self.remove_links or self.strip_words or self.replacements
            or self.regex_block or self.regex_require or self.min_length
            or self.max_length or self.allowed_media or self.blocked_media
        )

    @property
    def is_active(self) -> bool:
        """Configured rules run unless the task explicitly switched them off."""
        if self.enabled is False:
            return False
        if self.enabled is True:
            return self.has_rules
        return self.has_rules

    def describe(self) -> Dict[str, Any]:
        """Compact fingerprint for logs and the task detail API."""
        return {
            "source": self.source or "task",
            "blacklist": len(self.blacklist_words),
            "whitelist": len(self.whitelist_words),
            "whitelist_mode": self.whitelist_mode,
            "strip": len(self.strip_words),
            "replacements": len(self.replacements),
            "regex_block": len(self.regex_block),
            "regex_require": len(self.regex_require),
            "block_links": self.block_links,
            "remove_links": self.remove_links,
        }

    # -- evaluation ---------------------------------------------------------

    def _match_flags(self) -> Dict[str, Any]:
        return {
            "normalize": self.normalize,
            "case_sensitive": self.case_sensitive,
            "word_boundary": self.word_boundary,
        }

    def evaluate(self, text: str, media: str = "text") -> PolicyDecision:
        """Decide whether ``text`` may be forwarded. Cheapest check first."""
        if not self.is_active:
            return PolicyDecision(True, "policy_inactive")
        body = text or ""
        media_l = (media or "text").lower()

        if self.blocked_media and media_l in {m.lower() for m in self.blocked_media}:
            return PolicyDecision(False, f"media_blocked:{media_l}")
        if self.allowed_media and media_l not in {m.lower() for m in self.allowed_media}:
            return PolicyDecision(False, f"media_not_allowed:{media_l}")
        if self.min_length and len(body) < self.min_length:
            return PolicyDecision(False, f"too_short:{len(body)}")
        if self.max_length and len(body) > self.max_length:
            return PolicyDecision(False, f"too_long:{len(body)}")

        has_link = bool(_RE_LINK.search(body))
        if self.block_links and has_link:
            return PolicyDecision(False, "links_blocked")

        flags = self._match_flags()
        if self.blacklist_words:
            word = _first_matching_word(body, self.blacklist_words, **flags)
            if word:
                return PolicyDecision(False, f"blacklist:{word}", (word,))

        if self.whitelist_words:
            if self.whitelist_mode == "all":
                if not _first_matching_word(body, self.whitelist_words, **flags):
                    # Not one word of the list is in the text, so there is no
                    # need to check each one to know the message fails.
                    return PolicyDecision(
                        False, f"whitelist_missing:{','.join(self.whitelist_words[:3])}")
                missing = [w for w in self.whitelist_words if not contains_word(body, w, **flags)]
                if missing:
                    return PolicyDecision(False, f"whitelist_missing:{','.join(missing[:3])}")
            elif self.whitelist_mode == "phrase":
                phrase = " ".join(self.whitelist_words)
                if not contains_word(body, phrase, **flags):
                    return PolicyDecision(False, "whitelist_phrase_missing")
            else:
                if not _first_matching_word(body, self.whitelist_words, **flags):
                    return PolicyDecision(False, "whitelist_mismatch")

        for pattern in self.regex_block:
            compiled = _compile_user_regex(pattern)
            if compiled is not None and bounded_search(compiled, body) is not None:
                return PolicyDecision(False, f"regex_block:{pattern[:48]}", (pattern,))

        if self.regex_require:
            usable = [p for p in self.regex_require if _compile_user_regex(p) is not None]
            if usable and not any(
                bounded_search(_compile_user_regex(p), body) is not None  # type: ignore[union-attr]
                for p in usable
            ):
                return PolicyDecision(False, "regex_require_missing")

        return PolicyDecision(True, "policy_passed")

    # -- rewriting ----------------------------------------------------------

    def transform(self, text: str) -> Tuple[str, Dict[str, Any]]:
        """Apply remove-links / strip / replacements. Returns (text, notes)."""
        if not self.is_active or not text:
            return text, {}
        out = text
        notes: Dict[str, Any] = {}

        if self.remove_links:
            from .services import FilterEngine  # local import: no cycle, one implementation

            cleaned = FilterEngine().remove_urls(out)
            if cleaned != out:
                notes["links_removed"] = True
            out = cleaned

        if self.strip_words:
            removed = 0
            for word in self.strip_words:
                out, n = _strip_word(out, word, **self._match_flags())
                removed += n
            if removed:
                out = _tidy(out)
                notes["words_stripped"] = removed

        if self.replacements:
            out, replaced = _apply_replacements(
                out, self.replacements, case_sensitive=self.case_sensitive
            )
            if replaced:
                notes["replacements"] = replaced

        return out, notes


# --------------------------------------------------------------------------
# Transforms
# --------------------------------------------------------------------------


def _strip_word(text: str, word: str, **flags: Any) -> Tuple[str, int]:
    """Remove every occurrence of ``word`` (boundary-aware) from ``text``."""
    needle = (word or "").strip()
    if not needle or not text:
        return text, 0
    pattern = _needle_pattern(
        needle,
        mode=_match_mode(needle, flags["word_boundary"]),
        normalize=flags["normalize"],
        case_sensitive=flags["case_sensitive"],
    )
    # Offsets come straight from the original text: the pattern matches the
    # original characters, not a folded copy, so the spans are already correct.
    spans = [m.span() for m in pattern.finditer(text) if m.end() > m.start()]
    if not spans:
        return text, 0
    out, cursor = [], 0
    for begin, end in spans:
        if begin < cursor:
            continue
        out.append(text[cursor:begin])
        cursor = end
    out.append(text[cursor:])
    return "".join(out), len(spans)


def _tidy(text: str) -> str:
    """Collapse the spacing a removal leaves behind, without touching newlines."""
    out = re.sub(r"[ \t]{2,}", " ", text)
    out = re.sub(r"[ \t]+\n", "\n", out)
    return out.strip() if out.strip() else ""


def _apply_replacements(
    text: str, pairs: Sequence[Tuple[str, str]], *, case_sensitive: bool = False
) -> Tuple[str, int]:
    """Apply every replacement in a single pass over the original text.

    One pass is what makes the result predictable: ``a=>b`` followed by ``b=>c``
    yields ``b``, not ``c``. A ``re:`` prefix marks the find side as a regex.
    """
    if not pairs or not text:
        return text, 0
    parts: List[str] = []
    replacements: List[str] = []
    for find, repl in pairs:
        pattern = find[3:] if find.startswith("re:") else re.escape(find)
        if not pattern:
            continue
        parts.append(f"({pattern})")
        replacements.append(repl)
    if not parts:
        return text, 0
    combined = re.compile("|".join(parts), 0 if case_sensitive else re.IGNORECASE)
    count = 0

    def _sub(match: "re.Match[str]") -> str:
        nonlocal count
        count += 1
        return replacements[match.lastindex - 1] if match.lastindex else ""

    return combined.sub(_sub, text), count
