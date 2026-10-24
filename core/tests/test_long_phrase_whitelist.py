"""A multi-word whitelist phrase must match across ordinary whitespace.

The reported defect: in ``@AutoForwarderFreeBot`` short keywords such as ``5x``
and ``10x`` forward their messages, but a configured long phrase such as
``Whale Update on a VIP call`` never does.

Root cause (traced, not assumed):

``ContentPolicy``'s matcher (:func:`content_policy.contains_word`) compiles a
keyword into a variant-aware pattern that treats the space between a phrase's
words as ``\\s+`` — the *matcher* was never the problem. But ``any`` mode (and
blacklist) reach it through :func:`content_policy._first_matching_word`, which
first asks a cheap substring question — ``folded_needle in folded_body`` — and
skips the matcher entirely when it answers ``False``. Neither side of that
question folded whitespace: the needle ``"whale update on a vip call"`` is not
a substring of a body with a double space, a newline or a tab between the
phrase's words, so the pre-filter rejected the very messages the matcher was
built to accept. Short keywords carry no internal whitespace, so the same
pre-filter always passed them — which is exactly why ``5x`` worked and the
phrase did not, and why the failure looked length-related without being so.

The legacy ``FilterEngine._kw`` had its own copy of the same idea (a raw
``keyword.lower() in lowered_text``), with the same blindness to whitespace and
no folding at all.

What these tests pin:

* the exact reported phrase forwards, in text and in a media caption;
* short keywords keep forwarding (no regression to the working half);
* case, repeated spaces, newlines, tabs and trailing punctuation all match;
* ``any``, ``all`` and ``phrase`` modes keep their documented semantics;
* the same fix holds for the blacklist (a phrase blocks across whitespace);
* the legacy ``FilterRule`` keyword path (``FilterEngine``) behaves the same
  way, without losing its substring semantics;
* settings survive a save/reload round-trip (persistence is not the bug);
* one task's phrase never filters another task's messages.

Every test drives the real durable pipeline (``rule_repo`` +
``ContentPolicy.evaluate``) or the real ``FilterEngine`` — never a matcher in
isolation.
"""

from __future__ import annotations

import asyncio

from core.application.pipeline import DurableMessagePipeline
from core.domain.entities import FilterRule, ForwardRule, MessagePayload
from core.domain.services import FilterEngine
from core.domain.value_objects import MediaType
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteFilterRuleRepository,
    SqliteForwardRuleRepository,
    SqliteMessageMapRepository,
    SqliteProcessedMessageRepository,
)

OWNER = 5094837833
RULE_ID = "rule-phrase-1"
RULE_ID_OTHER = "rule-phrase-2"
SOURCE = "-1004400615270"
TARGET = "-1003915313919"

#: The exact entry the bug report names.
PHRASE = "Whale Update on a VIP call"


class FakeQueue:
    """Records accepted deliveries."""

    def __init__(self):
        self.jobs: list[tuple[str, int, str, str]] = []

    async def enqueue_delivery(self, rule, payload, target_chat_id, text,
                               evaluation=None, job_kind="forward",
                               correlation_id="", branding=None):
        self.jobs.append((rule.id, payload.message_id, str(target_chat_id), text))
        return True


def make_rule(rule_id: str = RULE_ID, **kwargs) -> ForwardRule:
    rule = ForwardRule(
        id=rule_id,
        session_id="sess-a",
        source_chat_id=SOURCE,
        target_chat_id=TARGET,
        is_active=True,
        owner_user_id=OWNER,
    )
    # Each test drives exactly one gate; a duplicate-filter match would hide it.
    rule.duplicate_filters = False
    for key, value in kwargs.items():
        setattr(rule, key, value)
    return rule


def build(db, queue) -> DurableMessagePipeline:
    return DurableMessagePipeline(
        rule_repo=SqliteForwardRuleRepository(db),
        filter_repo=SqliteFilterRuleRepository(db),
        queue_manager=queue,
        msg_map_repo=SqliteMessageMapRepository(db),
        processed_repo=SqliteProcessedMessageRepository(db),
    )


async def seed(db, *rules) -> None:
    from core.domain.entities import TelegramSession
    from core.infrastructure.persistence.sqlite_repositories import SqliteSessionRepository
    from core.infrastructure.security.crypto import CryptoService

    await SqliteSessionRepository(db, CryptoService("test-key")).add(
        TelegramSession(id="sess-a", phone_number="+100****0000", is_active=True,
                        is_authorized=True, owner_user_id=OWNER)
    )
    repo = SqliteForwardRuleRepository(db)
    for rule in rules:
        await repo.add(rule)


def payload(message_id: int, text: str, chat_id: str = SOURCE,
            caption: str = "", media: MediaType = MediaType.TEXT) -> MessagePayload:
    return MessagePayload(
        message_id=message_id, chat_id=chat_id, text=text, caption=caption,
        media_type=media, has_media=media is not MediaType.TEXT,
    )


async def forwarded_ids(db, queue, text: str, *, rule_id: str = RULE_ID,
                        message_id: int = 1, caption: str = "",
                        media: MediaType = MediaType.TEXT) -> list[int]:
    """Run one message through the real pipeline and report what was delivered."""
    queue.jobs.clear()
    pipe = build(db, queue)
    await pipe.process(
        payload(message_id, text, chat_id=SOURCE, caption=caption, media=media),
        trigger="NEW_MESSAGE",
    )
    return [job[1] for job in queue.jobs]


def _write_through_the_menu(rule: ForwardRule, *, blacklist=None, whitelist=None,
                            mode=None) -> None:
    """The exact storage writes the bot's ``Add`` prompt and menus perform."""
    if blacklist is not None:
        rule.blacklist = blacklist
    if whitelist is not None:
        rule.whitelist = whitelist
    if mode is not None:
        rule.whitelist_mode = mode


async def _reload(db, rule: ForwardRule) -> ForwardRule:
    """Re-read the rule the way a restart would (fresh repo, fresh object)."""
    stored = await SqliteForwardRuleRepository(db).get_by_id(rule.id)
    assert stored is not None
    return stored


def _run(coro):
    asyncio.run(coro)


# --------------------------------------------------------------------------- #
# The reported defect: the configured phrase must forward
# --------------------------------------------------------------------------- #
class TestTheReportedPhraseForwards:
    def test_the_exact_phrase_forwards(self, tmp_path):
        async def scenario():
            db = SqliteDatabase(str(tmp_path / "wl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, whitelist=[PHRASE], mode="any")
                await seed(db, rule)
                assert await forwarded_ids(db, FakeQueue(), PHRASE) == [1]
            finally:
                db.close()

        _run(scenario())

    def test_the_phrase_inside_a_longer_message_forwards(self, tmp_path):
        async def scenario():
            db = SqliteDatabase(str(tmp_path / "wl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, whitelist=[PHRASE], mode="any")
                await seed(db, rule)
                text = f"🚨 {PHRASE} — join now, seats are limited!"
                assert await forwarded_ids(db, FakeQueue(), text) == [1]
            finally:
                db.close()

        _run(scenario())

    def test_a_message_without_the_phrase_is_not_forwarded(self, tmp_path):
        async def scenario():
            db = SqliteDatabase(str(tmp_path / "wl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, whitelist=[PHRASE], mode="any")
                await seed(db, rule)
                assert await forwarded_ids(db, FakeQueue(), "an ordinary update") == []
            finally:
                db.close()

        _run(scenario())


# --------------------------------------------------------------------------- #
# Short keywords keep working (no regression to the working half)
# --------------------------------------------------------------------------- #
class TestShortKeywordsStillWork:
    def test_5x_and_10x_forward_their_messages(self, tmp_path):
        async def scenario():
            db = SqliteDatabase(str(tmp_path / "wl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, whitelist=["5x", "10x"], mode="any")
                await seed(db, rule)
                assert await forwarded_ids(db, FakeQueue(), "5x signal now",
                                           message_id=1) == [1]
                assert await forwarded_ids(db, FakeQueue(), "10x signal today",
                                           message_id=2) == [2]
            finally:
                db.close()

        _run(scenario())

    def test_short_and_phrase_entries_coexist(self, tmp_path):
        """A list holding both kinds must match either kind — mode ``any``."""
        async def scenario():
            db = SqliteDatabase(str(tmp_path / "wl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, whitelist=["5x", "10x", PHRASE], mode="any")
                await seed(db, rule)
                assert await forwarded_ids(db, FakeQueue(), "5x signal",
                                           message_id=1) == [1]
                assert await forwarded_ids(db, FakeQueue(), f"{PHRASE} soon",
                                           message_id=2) == [2]
            finally:
                db.close()

        _run(scenario())


# --------------------------------------------------------------------------- #
# Whitespace, case and punctuation variance — the pre-filter regression
# --------------------------------------------------------------------------- #
class TestWhitespaceCaseAndPunctuationVariance:
    @staticmethod
    def _db_with_phrase(tmp_path):
        db = SqliteDatabase(str(tmp_path / "wl.db"))
        rule = make_rule()
        _write_through_the_menu(rule, whitelist=["5x", "10x", PHRASE], mode="any")
        return db, rule

    def test_repeated_spaces_inside_the_phrase(self, tmp_path):
        async def scenario():
            db, rule = self._db_with_phrase(tmp_path)
            try:
                await seed(db, rule)
                text = f"🚨 Whale Update on a VIP  call — join!"
                assert await forwarded_ids(db, FakeQueue(), text) == [1]
            finally:
                db.close()

        _run(scenario())

    def test_a_newline_inside_the_phrase(self, tmp_path):
        async def scenario():
            db, rule = self._db_with_phrase(tmp_path)
            try:
                await seed(db, rule)
                text = "🚨 Whale Update on a VIP\ncall — join!"
                assert await forwarded_ids(db, FakeQueue(), text) == [1]
            finally:
                db.close()

        _run(scenario())

    def test_a_tab_inside_the_phrase(self, tmp_path):
        async def scenario():
            db, rule = self._db_with_phrase(tmp_path)
            try:
                await seed(db, rule)
                text = "Whale Update on a VIP\tcall!"
                assert await forwarded_ids(db, FakeQueue(), text) == [1]
            finally:
                db.close()

        _run(scenario())

    def test_case_variations_all_match(self, tmp_path):
        async def scenario():
            db, rule = self._db_with_phrase(tmp_path)
            try:
                await seed(db, rule)
                for idx, text in enumerate(
                    [PHRASE.upper(), PHRASE.lower(), PHRASE.title()], start=1
                ):
                    assert await forwarded_ids(db, FakeQueue(), text,
                                               message_id=idx) == [idx]
            finally:
                db.close()

        _run(scenario())

    def test_trailing_punctuation_does_not_break_the_match(self, tmp_path):
        async def scenario():
            db, rule = self._db_with_phrase(tmp_path)
            try:
                await seed(db, rule)
                assert await forwarded_ids(db, FakeQueue(), f"{PHRASE}.") == [1]
                assert await forwarded_ids(db, FakeQueue(), f"“{PHRASE}”",
                                           message_id=2) == [2]
            finally:
                db.close()

        _run(scenario())

    def test_a_media_caption_carrying_the_phrase_forwards(self, tmp_path):
        async def scenario():
            db, rule = self._db_with_phrase(tmp_path)
            try:
                await seed(db, rule)
                queue = FakeQueue()
                queue.jobs.clear()
                pipe = build(db, queue)
                await pipe.process(
                    payload(1, "", chat_id=SOURCE, caption=f"🚨 {PHRASE}!",
                            media=MediaType.PHOTO),
                    trigger="NEW_MESSAGE",
                )
                assert [job[1] for job in queue.jobs] == [1]
            finally:
                db.close()

        _run(scenario())


# --------------------------------------------------------------------------- #
# any / all / phrase semantics stay exactly what the registry documents
# --------------------------------------------------------------------------- #
class TestWhitelistModeSemantics:
    def test_any_mode_matches_one_entry(self, tmp_path):
        async def scenario():
            db = SqliteDatabase(str(tmp_path / "wl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, whitelist=["5x", PHRASE], mode="any")
                await seed(db, rule)
                assert await forwarded_ids(db, FakeQueue(), "only 5x here",
                                           message_id=1) == [1]
            finally:
                db.close()

        _run(scenario())

    def test_all_mode_requires_every_entry(self, tmp_path):
        async def scenario():
            db = SqliteDatabase(str(tmp_path / "wl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, whitelist=["5x", PHRASE], mode="all")
                await seed(db, rule)
                text = f"5x and {PHRASE} — both present"
                assert await forwarded_ids(db, FakeQueue(), text, message_id=1) == [1]
                assert await forwarded_ids(db, FakeQueue(), "only 5x here",
                                           message_id=2) == []
            finally:
                db.close()

        _run(scenario())

    def test_phrase_mode_matches_the_words_in_order(self, tmp_path):
        async def scenario():
            db = SqliteDatabase(str(tmp_path / "wl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, whitelist=[PHRASE], mode="phrase")
                await seed(db, rule)
                assert await forwarded_ids(db, FakeQueue(), PHRASE,
                                           message_id=1) == [1]
                # Same words, out of order: not the phrase.
                assert await forwarded_ids(
                    db, FakeQueue(), "a VIP call on Whale Update", message_id=2
                ) == []
            finally:
                db.close()

        _run(scenario())


# --------------------------------------------------------------------------- #
# The blacklist has the same duplicated logic — same fix, same guarantee
# --------------------------------------------------------------------------- #
class TestBlacklistPhrasesBlockAcrossWhitespace:
    def test_a_phrase_blacklist_blocks_the_exact_phrase(self, tmp_path):
        async def scenario():
            db = SqliteDatabase(str(tmp_path / "bl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, blacklist=[PHRASE])
                await seed(db, rule)
                assert await forwarded_ids(db, FakeQueue(), PHRASE) == []
                assert await forwarded_ids(db, FakeQueue(), "an ordinary update",
                                           message_id=2) == [2]
            finally:
                db.close()

        _run(scenario())

    def test_a_phrase_blacklist_blocks_across_repeated_spaces(self, tmp_path):
        async def scenario():
            db = SqliteDatabase(str(tmp_path / "bl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, blacklist=[PHRASE])
                await seed(db, rule)
                assert await forwarded_ids(db, FakeQueue(),
                                           "Whale Update on a VIP  call!") == []
            finally:
                db.close()

        _run(scenario())

    def test_a_phrase_blacklist_blocks_across_a_newline(self, tmp_path):
        async def scenario():
            db = SqliteDatabase(str(tmp_path / "bl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, blacklist=[PHRASE])
                await seed(db, rule)
                assert await forwarded_ids(db, FakeQueue(),
                                           "Whale Update on a VIP\ncall!") == []
            finally:
                db.close()

        _run(scenario())

    def test_blacklist_wins_over_whitelist(self, tmp_path):
        async def scenario():
            db = SqliteDatabase(str(tmp_path / "bl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, whitelist=[PHRASE], blacklist=[PHRASE])
                await seed(db, rule)
                assert await forwarded_ids(db, FakeQueue(), PHRASE) == []
            finally:
                db.close()

        _run(scenario())


# --------------------------------------------------------------------------- #
# Persistence: settings survive a reload (the menu write, not a shadow copy)
# --------------------------------------------------------------------------- #
class TestSettingsPersistAcrossReload:
    def test_the_phrase_survives_a_restart_and_still_forwards(self, tmp_path):
        async def scenario():
            db = SqliteDatabase(str(tmp_path / "wl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, whitelist=[PHRASE], mode="any")
                await seed(db, rule)
                reloaded = await _reload(db, rule)
                assert list(reloaded.whitelist) == [PHRASE], \
                    "the phrase must be stored as one entry"
                assert reloaded.whitelist_mode == "any"
                assert await forwarded_ids(db, FakeQueue(),
                                           f"{PHRASE} — reload!", message_id=2) == [2]
            finally:
                db.close()

        _run(scenario())

    def test_a_reloaded_phrase_matches_across_whitespace(self, tmp_path):
        async def scenario():
            db = SqliteDatabase(str(tmp_path / "wl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, whitelist=[PHRASE], mode="any")
                await seed(db, rule)
                # The reload is the whole point: the decision is re-read from
                # the stored blob, not held over from the in-memory object.
                await _reload(db, rule)
                assert await forwarded_ids(db, FakeQueue(),
                                           "Whale Update on a VIP\ncall!",
                                           message_id=3) == [3]
            finally:
                db.close()

        _run(scenario())


# --------------------------------------------------------------------------- #
# Task isolation: one task's phrase never filters another task
# --------------------------------------------------------------------------- #
class TestTaskIsolation:
    def test_an_unrelated_task_is_not_bound_by_anothers_phrase(self, tmp_path):
        async def scenario():
            db = SqliteDatabase(str(tmp_path / "wl.db"))
            try:
                rule = make_rule(RULE_ID, source_chat_id=SOURCE)
                _write_through_the_menu(rule, whitelist=[PHRASE], mode="any")
                other = make_rule(RULE_ID_OTHER, source_chat_id="-100999000111")
                _write_through_the_menu(other, whitelist=["5x"], mode="any")
                await seed(db, rule, other)
                # The phrase task drops a message the other task never sees a
                # keyword for — and the other task forwards a message the
                # phrase task drops. Neither leak is acceptable.
                queue = FakeQueue()
                queue.jobs.clear()
                pipe = build(db, queue)
                await pipe.process(payload(1, "an ordinary update", chat_id=SOURCE),
                                   trigger="NEW_MESSAGE")
                assert [job[0] for job in queue.jobs] == []
                await pipe.process(
                    payload(2, "5x signal", chat_id="-100999000111"),
                    trigger="NEW_MESSAGE",
                )
                assert [job[0] for job in queue.jobs] == [RULE_ID_OTHER]
                await pipe.process(
                    payload(3, f"{PHRASE} — finally", chat_id=SOURCE),
                    trigger="NEW_MESSAGE",
                )
                assert [job[0] for job in queue.jobs] == [RULE_ID_OTHER, RULE_ID]
            finally:
                db.close()

        _run(scenario())


# --------------------------------------------------------------------------- #
# The legacy FilterRule keyword path (FilterEngine) gets the same fix
# --------------------------------------------------------------------------- #
class TestLegacyFilterEnginePhraseKeywords:
    @staticmethod
    def _evaluate(text, *, whitelist=None, blacklist=None):
        engine = FilterEngine()
        if whitelist is not None:
            rule = FilterRule(id="f", name="f", whitelist_keywords=list(whitelist))
        else:
            rule = FilterRule(id="f", name="f", blacklist_keywords=list(blacklist or []))
        return engine.evaluate(
            MessagePayload(message_id=1, chat_id="-1001", chat_type="channel",
                           media_type=MediaType.TEXT, text=text),
            rule,
        )

    def test_a_legacy_whitelist_phrase_matches_across_whitespace(self):
        assert self._evaluate("Whale Update on a VIP call!",
                              whitelist=[PHRASE]).action.name == "ALLOW"
        assert self._evaluate("Whale Update on a VIP  call!",
                              whitelist=[PHRASE]).action.name == "ALLOW"
        assert self._evaluate("Whale Update on a VIP\ncall!",
                              whitelist=[PHRASE]).action.name == "ALLOW"

    def test_a_legacy_whitelist_phrase_without_a_match_drops(self):
        assert self._evaluate("unrelated text",
                              whitelist=[PHRASE]).action.name == "DROP"

    def test_a_legacy_blacklist_phrase_blocks_across_whitespace(self):
        assert self._evaluate("Whale Update on a VIP  call!",
                              blacklist=[PHRASE]).action.name == "DROP"
        assert self._evaluate(" Whale UPDATE on a VIP\ncall. ",
                              blacklist=[PHRASE]).action.name == "DROP"
        assert self._evaluate("clean text", blacklist=[PHRASE]).action.name == "ALLOW"

    def test_legacy_short_keywords_keep_their_substring_behaviour(self):
        """``5x`` inside ``a5x`` matched before the fix; it still must."""
        assert self._evaluate("a5x signal", whitelist=["5x"]).action.name == "ALLOW"
        assert self._evaluate("5x signal", whitelist=["5x"]).action.name == "ALLOW"


# --------------------------------------------------------------------------- #
# Input parsing: one canonical splitter, commas and newlines and the Persian
# comma all separate entries — a phrase is never cut in half
# --------------------------------------------------------------------------- #
class TestSplitWordList:
    def test_commas_and_newlines_separate_entries(self):
        from core.domain.content_policy import split_word_list

        assert split_word_list("5x, 10x") == ("5x", "10x")
        assert split_word_list("5x\n10x") == ("5x", "10x")
        assert split_word_list("5x\r\n10x") == ("5x", "10x")
        assert split_word_list(f"5x\n{PHRASE}") == ("5x", PHRASE)

    def test_the_persian_comma_separates_entries(self):
        from core.domain.content_policy import split_word_list

        assert split_word_list("5x\u060c 10x") == ("5x", "10x")

    def test_an_entry_keeps_its_internal_spaces(self):
        from core.domain.content_policy import split_word_list

        assert split_word_list(PHRASE) == (PHRASE,)
        assert split_word_list([PHRASE]) == (PHRASE,)

    def test_entries_are_stripped_and_deduped_in_order(self):
        from core.domain.content_policy import split_word_list

        assert split_word_list("  a , b , a ,\n b ") == ("a", "b")

    def test_empty_input_yields_nothing(self):
        from core.domain.content_policy import split_word_list

        assert split_word_list("") == ()
        assert split_word_list(None) == ()
        assert split_word_list(" , \n ") == ()

    def test_from_dict_parses_a_comma_blob_with_the_persian_comma(self):
        from core.domain.content_policy import ContentPolicy

        policy = ContentPolicy.from_dict({"whitelist_words": "5x\u060c 10x"})
        assert policy.whitelist_words == ("5x", "10x")
