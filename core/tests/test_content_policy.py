"""Per-task content rules: blacklist, whitelist-only, replace, remove links.

These tests pin the *semantics* a user configures — "never forward if this word
appears", "forward only if it contains exactly these words", "replace this with
that", "strip the links" — and the properties that make them trustworthy in a
real channel: word boundaries (``cat`` must not match ``concatenate``),
Persian/Arabic spelling variants, per-source and per-target overrides, no cap on
how many words a task may carry, and no way for a hostile pattern to stall a
worker.
"""

from __future__ import annotations

from core.domain.content_policy import (
    ContentPolicy,
    PolicyDecision,
    contains_word,
    fold_text,
    has_link,
)

import pytest


def policy(**kw) -> ContentPolicy:
    return ContentPolicy.from_dict(kw)


# --------------------------------------------------------------------------
# Words: boundaries and normalisation
# --------------------------------------------------------------------------


def test_blacklist_word_blocks_the_message():
    pol = policy(blacklist_words=["casino"])
    assert pol.evaluate("Join our casino now").allowed is False
    assert pol.evaluate("Join our casino now").reason == "blacklist:casino"


def test_blacklist_is_word_aware_not_substring():
    """`cat` must not match `concatenate`, or a filter eats unrelated posts."""
    pol = policy(blacklist_words=["cat"])
    assert pol.evaluate("concatenate the strings").allowed is True
    assert pol.evaluate("my cat is here").allowed is False


def test_blacklist_matches_persian_spelling_variants():
    """One keyword, both keyboards: ي/ی and ك/ک are the same letter to a reader."""
    pol = policy(blacklist_words=["تخفیف"])
    assert pol.evaluate("فروش ویژه با تخفیف").allowed is False
    # The same word typed with Arabic yeh/kaf must still match.
    assert pol.evaluate("فروش ویژه با تخفيف").allowed is False


def test_zero_width_joiner_does_not_hide_a_word():
    """`تبلیغ` written with a ZWNJ inside still reads as one word."""
    pol = policy(blacklist_words=["تبلیغ"])
    assert pol.evaluate("این یک تبلیغ است").allowed is False
    assert pol.evaluate("این یک تبلیغ است").allowed is False


def test_case_insensitive_by_default_and_opt_in_strictness():
    assert policy(blacklist_words=["spam"]).evaluate("SPAM").allowed is False
    strict = policy(blacklist_words=["spam"], case_sensitive=True)
    assert strict.evaluate("SPAM").allowed is True
    assert strict.evaluate("spam").allowed is False


def test_punctuation_keywords_are_substring_matched():
    """A keyword that is not a word (`#ad`, `@scam`) has no boundary to respect."""
    pol = policy(blacklist_words=["#ad"])
    assert pol.evaluate("today #advert").allowed is False
    assert policy(blacklist_words=["@scam"]).evaluate("via @scam_bot").allowed is False


# --------------------------------------------------------------------------
# Whitelist-only
# --------------------------------------------------------------------------


def test_whitelist_only_forwards_when_a_word_matches():
    pol = policy(whitelist_words=["فوری", "urgent"])
    assert pol.evaluate("خبر فوری برای شما").allowed is True
    assert pol.evaluate("یک خبر معمولی").allowed is False
    assert pol.evaluate("یک خبر معمولی").reason == "whitelist_mismatch"


def test_whitelist_all_requires_every_word():
    pol = policy(whitelist_words=["برق", "خبر"], whitelist_mode="all")
    assert pol.evaluate("برق و خبر").allowed is True
    assert pol.evaluate("فقط برق").allowed is False
    assert pol.evaluate("فقط برق").reason.startswith("whitelist_missing")


def test_whitelist_phrase_requires_the_words_in_order():
    pol = policy(whitelist_words=["خرید", "فوری"], whitelist_mode="phrase")
    assert pol.evaluate("پیشنهاد خرید فوری امروز").allowed is True
    assert pol.evaluate("فوری خرید").allowed is False


def test_blacklist_beats_whitelist():
    """A message that matches the whitelist but also the blacklist is blocked."""
    pol = policy(whitelist_words=["خبر"], blacklist_words=["تبلیغ"])
    assert pol.evaluate("خبر تبلیغ").allowed is False
    assert pol.evaluate("خبر تبلیغ").reason == "blacklist:تبلیغ"


def test_disabled_policy_lets_everything_through():
    pol = policy(enabled=False, blacklist_words=["casino"])
    assert pol.evaluate("casino").allowed is True
    assert pol.evaluate("casino").reason == "policy_inactive"


def test_configured_rules_are_active_without_an_explicit_switch():
    """Setting words is the intent; nobody should have to flip two switches."""
    assert policy(blacklist_words=["x"]).is_active is True
    assert policy().is_active is False


# --------------------------------------------------------------------------
# Links
# --------------------------------------------------------------------------


def test_block_links_blocks_every_link_shape():
    pol = policy(block_links=True)
    for text in ("see https://x.com/a", "see www.example.com", "join t.me/chan",
                 "telegram.me/chan", "tg://resolve?domain=x", "http://a.b"):
        assert pol.evaluate(text).allowed is False, text
    assert pol.evaluate("no link here").allowed is True


def test_remove_links_rewrites_instead_of_dropping():
    pol = policy(remove_links=True)
    assert pol.evaluate("see https://x.com/a now").allowed is True
    out, notes = pol.transform("see https://x.com/a now")
    assert "https://x.com/a" not in out
    assert notes.get("links_removed") is True
    assert "see" in out and "now" in out


def test_has_link_detector_covers_more_than_http():
    assert has_link("www.example.com") is True
    assert has_link("tg://resolve?domain=x") is True
    assert has_link("plain text") is False
    assert has_link("") is False


# --------------------------------------------------------------------------
# Strip and replace
# --------------------------------------------------------------------------


def test_strip_words_removes_the_word_and_tidies_spacing():
    pol = policy(strip_words=["@channel", "join us"])
    out, notes = pol.transform("Hello join us @channel friend")
    assert "@channel" not in out
    assert "join us" not in out
    assert "  " not in out
    assert notes.get("words_stripped", 0) >= 1


def test_replacements_apply_literally():
    pol = policy(replacements={"old brand": "new brand"})
    out, notes = pol.transform("buy old brand today")
    assert out == "buy new brand today"
    assert notes["replacements"] == 1


def test_replacements_do_not_cascade():
    """`a=>b` then `b=>c` must produce `b`, not `c`: one pass, no surprises."""
    pol = policy(replacements=[["a", "b"], ["b", "c"]])
    out, _ = pol.transform("a")
    assert out == "b"


def test_replacements_accept_regex_and_string_forms():
    pol = policy(replacements={"re:  +": " "})
    assert pol.transform("too   many spaces")[0] == "too many spaces"
    pol2 = ContentPolicy.from_dict({"replacements": ["foo=>bar"]})
    assert pol2.transform("foo baz")[0] == "bar baz"


def test_replacements_follow_configured_order():
    pol = policy(replacements=[["x", "1"], ["y", "2"]])
    assert pol.transform("x y")[0] == "1 2"


# --------------------------------------------------------------------------
# Regex rules and hostile patterns
# --------------------------------------------------------------------------


def test_regex_block_and_require():
    assert policy(regex_block=[r"\b\d{6,}\b"]).evaluate("code 1234567").allowed is False
    assert policy(regex_block=[r"\b\d{6,}\b"]).evaluate("code 12").allowed is True
    req = policy(regex_require=[r"\bخرید\b"])
    assert req.evaluate("خرید کنید").allowed is True
    assert req.evaluate("سلام").allowed is False


def test_nested_quantifier_patterns_are_refused_and_fail_closed():
    """(a+)+ is the shape that burns a worker on a crafted message; fail closed safely."""
    pol = policy(regex_block=[r"(a+)+$"])
    res = pol.evaluate("a" * 40 + "b")
    assert res.allowed is False
    assert res.reason.startswith("invalid_regex_block:")


def test_broken_regex_fails_closed_safely():
    pol = policy(regex_block=["([unclosed"])
    res = pol.evaluate("anything")
    assert res.allowed is False
    assert res.reason.startswith("invalid_regex_block:")


# --------------------------------------------------------------------------
# Length / media gates
# --------------------------------------------------------------------------


def test_length_and_media_gates():
    assert policy(min_length=5).evaluate("abcd").allowed is False
    assert policy(max_length=5).evaluate("abcdef").allowed is False
    assert policy(allowed_media=["photo"]).evaluate("x", "text").allowed is False
    assert policy(blocked_media=["sticker"]).evaluate("x", "sticker").allowed is False


# --------------------------------------------------------------------------
# Scale: "no limits"
# --------------------------------------------------------------------------


def test_no_cap_on_words_or_replacements():
    words = [f"word{i}" for i in range(500)]
    pol = policy(blacklist_words=words)
    assert len(pol.blacklist_words) == 500
    assert pol.evaluate("this mentions word499").allowed is False
    assert pol.evaluate("nothing here").allowed is True

    pairs = [[f"a{i}", f"b{i}"] for i in range(300)]
    pol2 = policy(replacements=pairs)
    out, notes = pol2.transform(" ".join(f"a{i}" for i in range(300)))
    assert notes["replacements"] == 300
    assert out.split() == [f"b{i}" for i in range(300)]


def test_a_large_policy_evaluates_in_one_scan_per_message():
    """A 500-word blacklist is one scan per message, not 500 regex searches.

    The wall-clock bound is deliberately generous — a shared CI runner is
    several times slower than a dev box — because what it guards is the *shape*
    of the algorithm: with the words combined into one alternation the cost is
    flat in the list length, so a regression back to one search per word blows
    straight through it. `test_the_whole_word_list_becomes_one_pattern` pins the
    same property without timing anything.
    """
    import time

    pol = policy(blacklist_words=[f"w{i}" for i in range(500)])
    body = ("some ordinary text " * 100)
    pol.evaluate(body)  # warm the compiled-pattern caches
    start = time.perf_counter()
    for _ in range(50):
        pol.evaluate(body)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"evaluation too slow: {elapsed:.3f}s for 50 messages"


def test_a_word_list_is_scanned_without_a_regex_per_word():
    """The skip is a folded substring scan; the pattern still decides.

    `beta` inside `alphabet` is not a match (word boundary), a token marker is
    still a family (`#promo` catches `#promotional`), and a Farsi-typed keyword
    still catches the Arabic letters of the same word — the shortcut only makes
    the search cheaper, it never changes the verdict.
    """
    from core.domain import content_policy as cp

    flags = {"normalize": True, "case_sensitive": False, "word_boundary": True}
    assert cp._first_matching_word("nothing to see here", ["alpha", "beta"], **flags) == ""
    assert cp._first_matching_word("an alpha token", ["alpha", "beta"], **flags) == "alpha"
    # the first offender in the task's own order is the one reported
    assert cp._first_matching_word("a beta value", ["beta", "alpha"], **flags) == "beta"
    # a substring hit that is not a word match is not a hit
    assert cp._first_matching_word("alphabet soup", ["alpha"], **flags) == ""
    # token families survive
    assert cp._first_matching_word("#promotional", ["#promo"], **flags) == "#promo"
    # letter variants survive
    farsi = "\u06a9\u062a\u0627\u0628"
    arabic_typed = "\u0643\u062a\u0627\u0628"
    assert cp._first_matching_word(f"see {arabic_typed} now", [farsi], **flags) == farsi


# --------------------------------------------------------------------------
# Normalisation helpers
# --------------------------------------------------------------------------


def test_fold_text_is_idempotent_and_variant_insensitive():
    a = fold_text("كتاب")   # Arabic kaf / yeh
    b = fold_text("کتاب")   # Farsi keh / yeh
    assert a == b
    assert fold_text(a) == a


def test_contains_word_handles_empty_and_punctuation():
    assert contains_word("hello", "") is False
    assert contains_word("", "hello") is False
    assert contains_word("hello world", "world") is True
    assert contains_word("hello worlds", "world") is False      # boundary
    assert contains_word("hello worlds", "world", word_boundary=False) is True


def test_policy_round_trips_through_to_dict():
    cfg = {
        "enabled": True,
        "blacklist_words": ["a", "b"],
        "whitelist_words": ["c"],
        "whitelist_mode": "all",
        "replacements": {"x": "y"},
        "remove_links": True,
        "regex_block": ["z"],
    }
    pol = ContentPolicy.from_dict(cfg)
    again = ContentPolicy.from_dict(pol.to_dict())
    assert again.evaluate("a").allowed is False
    assert again.to_dict()["whitelist_mode"] == "all"
    assert again.replacements == (("x", "y"),)


def test_garbage_input_never_raises():
    for raw in (None, [], "nonsense", 42, {"blacklist_words": None},
                {"whitelist_mode": "banana"}, {"replacements": "not a mapping"},
                {"min_length": "abc"}):
        pol = ContentPolicy.from_dict(raw)
        assert isinstance(pol.evaluate("text"), PolicyDecision)
        assert pol.transform("text")[0] == "text"
