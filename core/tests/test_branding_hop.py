"""Regression tests for the branding hop (A -> C -> B).

The feature existed in three halves that never met: the routing decision said
`BRANDING_VIA_C`, the dispatcher copied the message into C — and then handed the
delivery to the durable queue *without* the hop, so the target received a forward
of the original source (its header, not C's) while an orphan copy stayed in C.

These tests pin the three halves together:

* the decision (triggers, gates, and the config stored in ``metadata``),
* the dispatch hand-off (the hop travels to the queue as job state), and
* the worker (stage 2 is a forward of C, never of the source).
"""

from __future__ import annotations

import asyncio
import types
from typing import Any, Dict, List, Optional

import pytest

from core.domain import branding
from core.domain.entities import DeliveryJob, ForwardRule, MessagePayload
from core.domain.rule_settings import SETTING_SPECS, apply_settings, read_settings, schema
from core.domain.services import SmartRoutingEngine
from core.domain.value_objects import RoutingDecision, ForwardMode
from core.infrastructure.telegram.durable_queue import DurableQueueManager

C_CHANNEL = "-1001789"

def _payload(text: str = "hello world", origin: Optional[Dict[str, Any]] = None,
             message_id: int = 5) -> MessagePayload:
    payload = MessagePayload(chat_id="-1001234", message_id=message_id, text=text)
    if origin:
        payload.forward_origin = origin
    return payload


def _rule(**kw) -> ForwardRule:
    base = dict(
        id="rule-1", session_id="sess-1",
        source_chat_id="-1001234", target_chat_id="-1007777",
        use_intermediate=True, intermediate_channel_id=C_CHANNEL,
    )
    base.update(kw)
    return ForwardRule(**base)


# --------------------------------------------------------------------------- #
# Decision: enablement + triggers
# --------------------------------------------------------------------------- #
class TestBrandingDecision:
    def test_hop_requires_a_channel(self):
        rule = _rule(intermediate_channel_id="")
        hop, reason = branding.should_hop(_payload(), rule)
        assert hop is False
        assert reason == "branding_disabled:no_channel"

    def test_legacy_mode_spelling_also_enables_the_hop(self):
        """A rule switched on through the legacy spelling must still hop."""
        rule = _rule(use_intermediate=False, intermediate_mode="VIA_INTERMEDIATE")
        hop, _reason = branding.should_hop(_payload(), rule)
        assert hop is True

    def test_unconditional_hop_when_triggers_are_off(self):
        hop, reason = branding.should_hop(_payload(), _rule())
        assert hop is True, "an enabled hop with no triggers must be unconditional"
        assert reason == "branding_always"

    def test_origin_channel_name_is_the_trigger_the_operator_configures(self):
        """'if the forwarded-from header (channel NAME) is in this list'."""
        rule = _rule()
        rule.branding_triggers_enabled = True
        rule.branding_origin_titles = ["Premium Deals"]
        engine = SmartRoutingEngine()

        matching = _payload(origin={"from_chat_id": "-10055", "from_chat_title": "Premium Deals"},
                            message_id=6)
        assert engine.decide_route(matching, rule)[0] is RoutingDecision.BRANDING_VIA_C

        other = _payload(origin={"from_chat_id": "-10056", "from_chat_title": "Some Other Channel"},
                         message_id=7)
        assert engine.decide_route(other, rule)[0] is RoutingDecision.DIRECT_COPY

    def test_origin_id_and_username_are_accepted(self):
        rule = _rule()
        rule.branding_triggers_enabled = True
        rule.branding_origin_ids = ["-10055", "@newsdesk"]
        assert branding.should_hop(_payload(origin={"from_chat_id": "-10055"}), rule)[0] is True
        assert branding.should_hop(_payload(origin={"from_chat_username": "newsdesk"}), rule)[0] is True
        assert branding.should_hop(_payload(origin={"from_chat_id": "-10099"}), rule)[0] is False

    def test_keyword_trigger_scans_the_text(self):
        rule = _rule()
        rule.branding_triggers_enabled = True
        rule.branding_keywords = ["promo", "ويژه"]
        assert branding.should_hop(_payload("Big PROMO today"), rule)[0] is True
        assert branding.should_hop(_payload("این پیام ویژه است"), rule)[0] is True
        assert branding.should_hop(_payload("nothing to see"), rule)[0] is False

    def test_match_mode_all_requires_every_configured_trigger(self):
        rule = _rule()
        rule.branding_triggers_enabled = True
        rule.branding_keywords = ["promo"]
        rule.branding_origin_titles = ["Premium Deals"]
        rule.branding_match_mode = "all"

        both = _payload("promo", origin={"from_chat_title": "Premium Deals"})
        only_one = _payload("promo", origin={"from_chat_title": "Other"})
        assert branding.should_hop(both, rule)[0] is True
        assert branding.should_hop(only_one, rule)[0] is False

        rule.branding_match_mode = "any"
        assert branding.should_hop(only_one, rule)[0] is True

    def test_require_forward_is_a_gate(self):
        rule = _rule()
        rule.branding_triggers_enabled = True
        rule.branding_keywords = ["promo"]
        rule.branding_require_forward = True
        # The keyword matches, but the message is not a forward: no hop.
        assert branding.should_hop(_payload("promo"), rule) == (False, "branding_requires_forward_origin")
        assert branding.should_hop(_payload("promo", origin={"from_chat_id": "-10055"}), rule)[0] is True

    def test_legacy_vip_criteria_still_gate_the_hop(self):
        """A task that declared VIP criteria keeps hopping only for VIP messages."""
        rule = _rule(message_category="VIP")
        rule.detection_criteria = {"forward_origin_titles": ["Premium Deals"]}
        engine = SmartRoutingEngine()

        vip = _payload(origin={"from_chat_title": "Premium Deals"}, message_id=11)
        regular = _payload(origin={"from_chat_title": "Random"}, message_id=12)
        assert engine.should_brand_hop(vip, rule)[0] is True
        assert engine.should_brand_hop(regular, rule)[0] is False

    def test_preview_and_engine_agree(self):
        """The dashboard/bot preview must reproduce the engine's verdict."""
        rule = _rule()
        rule.branding_triggers_enabled = True
        rule.branding_keywords = ["promo"]
        engine = SmartRoutingEngine()
        for text in ("promo now", "nothing"):
            payload = _payload(text)
            hop, _reason = engine.should_brand_hop(payload, rule)
            route, _why = engine.decide_route(payload, rule)
            assert (route is RoutingDecision.BRANDING_VIA_C) is hop


# --------------------------------------------------------------------------- #
# Per-task configuration surface
# --------------------------------------------------------------------------- #
class TestBrandingSettings:
    def test_branding_group_is_registered_and_exposed_to_clients(self):
        keys = {k for k, spec in SETTING_SPECS.items() if spec.group == "branding"}
        assert keys == {
            "branding_triggers_enabled", "branding_origin_titles", "branding_origin_ids",
            "branding_keywords", "branding_match_mode", "branding_require_forward",
            "branding_force_forward",
        }
        published = {s["key"] for s in schema()["settings"]}
        assert keys <= published
        assert "branding" in schema()["groups"]

    def test_settings_round_trip_through_the_entity(self):
        rule = _rule()
        changed = apply_settings(rule, {
            "branding_triggers_enabled": True,
            "branding_origin_titles": ["Premium Deals", "Premium Deals", ""],
            "branding_keywords": ["promo"],
            "branding_match_mode": "all",
        })
        assert set(changed) == {
            "branding_triggers_enabled", "branding_origin_titles", "branding_keywords",
            "branding_match_mode",
        }
        stored = read_settings(rule)
        assert stored["branding_origin_titles"] == ["Premium Deals"]  # deduped, blanks dropped
        assert stored["branding_match_mode"] == "all"
        # It survives as JSON metadata, so backup/restore and every client carry it.
        assert rule.metadata["branding_hop"]["origin_titles"] == ["Premium Deals"]

    def test_settings_reject_an_unknown_match_mode(self):
        from core.domain.rule_settings import SettingError

        rule = _rule()
        with pytest.raises(SettingError):
            apply_settings(rule, {"branding_match_mode": "sometimes"})

    def test_config_is_readable_when_stored_metadata_is_malformed(self):
        rule = _rule()
        rule.metadata["branding_hop"] = "not-a-dict"
        assert branding.read_config(rule) == branding.DEFAULTS


# --------------------------------------------------------------------------- #
# The defect itself: the hop must survive the durable queue
# --------------------------------------------------------------------------- #
class _FakeClient:
    def __init__(self) -> None:
        self.forwards: List[Dict[str, Any]] = []
        self.copies: List[Dict[str, Any]] = []
        self.sends: List[Dict[str, Any]] = []
        self._next_id = 500

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def send_message(self, chat_id: Any, text: str, **kwargs) -> Any:
        message_id = self._id()
        self.sends.append({"chat_id": str(chat_id), "text": text, "id": message_id})
        return types.SimpleNamespace(id=message_id)

    async def copy_message(self, chat_id: Any, from_chat_id: Any, message_id: int,
                           caption: Optional[str] = None, **kwargs) -> Any:
        message_id_out = self._id()
        self.copies.append({
            "chat_id": str(chat_id), "from_chat_id": str(from_chat_id),
            "message_id": int(message_id), "id": message_id_out,
        })
        return types.SimpleNamespace(id=message_id_out)

    async def forward_messages(self, chat_id: Any, from_chat_id: Any,
                              message_ids: List[int], **kwargs) -> Any:
        message_id_out = self._id()
        self.forwards.append({
            "chat_id": str(chat_id), "from_chat_id": str(from_chat_id),
            "message_ids": [int(m) for m in message_ids], "id": message_id_out,
        })
        return [types.SimpleNamespace(id=message_id_out)]


class _FakePool:
    def __init__(self, client: _FakeClient) -> None:
        self.client = client

    def get(self, session_id: str) -> Optional[_FakeClient]:
        return self.client


class _FakeQueueManager:
    """Captures what the dispatcher hands to the durable queue."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def enqueue_delivery(self, rule, payload, target_chat_id, text,
                              evaluation=None, job_kind="forward", correlation_id="",
                              branding=None) -> bool:
        self.calls.append({
            "rule_id": rule.id, "target": str(target_chat_id), "text": text,
            "source_chat_id": str(payload.chat_id),
            "source_message_id": int(payload.message_id),
            "branding": dict(branding) if branding else None,
        })
        return True


class _FakeUseCase:
    def __init__(self) -> None:
        from core.domain.services import SmartRoutingEngine

        self.sender = None
        self._routing = SmartRoutingEngine()


def _dispatcher(client: _FakeClient, queue_manager=None):
    from core.infrastructure.telegram.dispatcher import MessageDispatcher

    use_case = _FakeUseCase()
    dispatcher = MessageDispatcher(pool=_FakePool(client), use_case=use_case,
                                   queue_manager=queue_manager)
    return dispatcher, queue_manager


class TestHopSurvivesTheQueue:
    @pytest.mark.asyncio
    async def test_dispatch_posts_to_c_once_and_passes_the_hop_to_the_queue(self):
        client = _FakeClient()
        queue = _FakeQueueManager()
        dispatcher, _ = _dispatcher(client, queue)
        rule = _rule(target_chat_ids=["-1007777", "-1008888"])

        assert await dispatcher._send(_payload(), rule, "hello", None) is True

        # Exactly one post into C, and no delivery to C as a target.
        c_posts = [c for c in client.copies + client.sends if c["chat_id"] == C_CHANNEL]
        assert len(c_posts) == 1, "the hop must post into C exactly once"
        hop_id = c_posts[0]["id"]

        # Both final targets were queued, each carrying the plan.
        assert [c["target"] for c in queue.calls] == ["-1007777", "-1008888"]
        for call in queue.calls:
            assert call["branding"] is not None, "the queue must receive the hop"
            assert call["branding"]["chat_id"] == C_CHANNEL
            assert call["branding"]["message_id"] == hop_id
            # The job identity stays the *original* message: the mapping and the
            # idempotency key are keyed on it, so a replay cannot post a second
            # copy into C.
            assert call["source_chat_id"] == "-1001234"

    @pytest.mark.asyncio
    async def test_a_gated_out_message_is_queued_without_the_hop(self):
        client = _FakeClient()
        queue = _FakeQueueManager()
        dispatcher, _ = _dispatcher(client, queue)
        rule = _rule()
        rule.branding_triggers_enabled = True
        rule.branding_keywords = ["promo"]

        await dispatcher._send(_payload("nothing interesting"), rule, "nothing interesting", None)

        assert not [c for c in client.copies + client.sends if c["chat_id"] == C_CHANNEL], \
            "a trigger miss must not post into C at all"
        assert queue.calls and queue.calls[0]["branding"] is None

    @pytest.mark.asyncio
    async def test_direct_path_forwards_from_c_not_from_the_source(self):
        client = _FakeClient()
        dispatcher, _ = _dispatcher(client)  # no queue manager -> direct delivery
        rule = _rule()

        await dispatcher._send(_payload(), rule, "hello", None)

        final = [f for f in client.forwards if f["chat_id"] == "-1007777"]
        assert final, "the target must receive a forward"
        assert final[0]["from_chat_id"] == C_CHANNEL, "stage 2 must forward C, never A"


class _WorkerStub(DurableQueueManager):
    """The worker's send path, without spinning up the whole manager."""

    def __init__(self) -> None:  # noqa: D107 - deliberately no base init
        pass


class TestWorkerReproducesTheHop:
    @pytest.mark.asyncio
    async def test_branded_job_forwards_from_c(self):
        client = _FakeClient()
        worker = _WorkerStub()
        job = DeliveryJob(
            id="rule-1:-1001234:5:-1007777", rule_id="rule-1",
            source_chat_id="-1001234", source_message_id=5, target_chat_id="-1007777",
            payload_data={
                "forward_mode": ForwardMode.COPY_MESSAGE.value, "content_mode": "AUTO",
                "text": "hello", "has_media": False,
                "branding": {"chat_id": C_CHANNEL, "message_id": 77, "force_forward": True},
            },
        )

        await worker._dispatch_to_telegram(client, job)

        assert client.forwards == [{
            "chat_id": "-1007777", "from_chat_id": C_CHANNEL,
            "message_ids": [77], "id": client.forwards[0]["id"],
        }]
        assert client.copies == [], "a branded job must never read the original message"

    @pytest.mark.asyncio
    async def test_unbranded_job_still_copies_the_original(self):
        """The guard can fail: without the plan, the old (unbranded) path runs."""
        client = _FakeClient()
        worker = _WorkerStub()
        job = DeliveryJob(
            id="rule-1:-1001234:5:-1007777", rule_id="rule-1",
            source_chat_id="-1001234", source_message_id=5, target_chat_id="-1007777",
            payload_data={
                "forward_mode": ForwardMode.COPY_MESSAGE.value, "content_mode": "AUTO",
                "text": "hello", "has_media": True, "media_type": "photo",
            },
        )

        await worker._dispatch_to_telegram(client, job)

        assert client.forwards == []
        assert [c["from_chat_id"] for c in client.copies] == ["-1001234"]


class TestQueueRecordsTheHop:
    @pytest.mark.asyncio
    async def test_job_carries_the_branding_plan_and_stage(self):
        captured: Dict[str, Any] = {}

        async def _capture(mapping, job):
            captured["job"] = job
            captured["mapping"] = mapping
            return True

        manager = _WorkerStub()
        manager._max_queue_size = 100
        manager.stats = {"enqueued": 0, "backpressure_drops": 0}
        manager._enqueue_atomic = _capture
        manager._queue = types.SimpleNamespace(get_pending_count=lambda: _async(0))
        manager._notify_event = asyncio.Event()

        rule = _rule()
        payload = _payload(message_id=5)
        ok = await manager.enqueue_delivery(
            rule, payload, "-1007777", "hello", None,
            branding={"chat_id": C_CHANNEL, "message_id": 77, "force_forward": True},
        )

        assert ok is True
        job = captured["job"]
        assert job.payload_data["branding"] == {
            "chat_id": C_CHANNEL, "message_id": 77, "force_forward": True,
        }
        assert job.intermediate_chat_id == C_CHANNEL
        assert job.intermediate_message_id == 77
        assert job.delivery_stage == "BRANDED"
        # The mapping/idempotency key still names the original message.
        assert captured["mapping"].source_chat_id == "-1001234"
        assert captured["mapping"].source_message_id == 5

    @pytest.mark.asyncio
    async def test_unbranded_job_is_marked_direct(self):
        captured: Dict[str, Any] = {}

        async def _capture(mapping, job):
            captured["job"] = job
            return True

        manager = _WorkerStub()
        manager._max_queue_size = 100
        manager.stats = {"enqueued": 0, "backpressure_drops": 0}
        manager._enqueue_atomic = _capture
        manager._queue = types.SimpleNamespace(get_pending_count=lambda: _async(0))
        manager._notify_event = asyncio.Event()

        await manager.enqueue_delivery(_rule(), _payload(), "-1007777", "hello")

        assert captured["job"].payload_data["branding"] is None
        assert captured["job"].intermediate_chat_id is None
        assert captured["job"].delivery_stage == "DIRECT"


async def _async(value):
    return value


# --------------------------------------------------------------------------- #
# Bot panel: the operator configures the hop from the same config the engine reads
# --------------------------------------------------------------------------- #
def _bot_ui(lang: str = "en"):
    from core.infrastructure.telegram.i18n import I18n
    from core.infrastructure.telegram.pro_bot_ui import ProBotUI, UiState

    ui = ProBotUI.__new__(ProBotUI)
    ui._states = {5094837833: UiState(lang=lang)}
    ui._users = None
    ui.i18n = I18n()
    ui._t = ProBotUI._t.__get__(ui, ProBotUI)
    ui._kbd = lambda rows: ProBotUI._kbd(ui, rows)
    return ui


def _menu_callbacks(kbd) -> List[str]:
    return [str(b.callback_data or "") for row in kbd.inline_keyboard for b in row]


class TestBotBrandingPanel:
    def test_panel_exposes_the_branding_controls(self):
        ui = _bot_ui("en")
        rule = _rule(id="rule_brand_1234")
        text, kbd = ui._render_smart_menu(rule)
        callbacks = _menu_callbacks(kbd)

        for prefix in ("rsmbo:", "rsmbk:", "rsmbr:", "rsmbf:"):
            assert any(cb.startswith(prefix) for cb in callbacks), f"{prefix} button missing"
        assert "Branding hop" in text
        # Nothing configured yet: the hop applies to every message.
        assert "every message" in text

    def test_panel_reflects_the_stored_config(self):
        ui = _bot_ui("en")
        rule = _rule(id="rule_brand_5678")
        branding.write_config(rule, {
            "triggers_enabled": True, "origin_titles": ["Premium Deals"],
            "keywords": ["promo"], "require_forward": True,
        })
        text, kbd = ui._render_smart_menu(rule)

        assert "Premium Deals" in text and "promo" in text
        assert "filtered" in text
        callbacks = _menu_callbacks(kbd)
        # The toggle buttons show the current state, so the operator can see it.
        gate = [b.text for row in kbd.inline_keyboard for b in row if b.callback_data.startswith("rsmbr:")]
        assert gate and "ON" in gate[0]

    def test_every_callback_fits_the_telegram_budget(self):
        """Callback data must stay under Telegram's 64-byte limit."""
        ui = _bot_ui("en")
        rule = _rule(id="rule_brand_1234")
        _text, kbd = ui._render_smart_menu(rule)
        for cb in _menu_callbacks(kbd):
            assert len(cb.encode("utf-8")) <= 64, cb

    @pytest.mark.parametrize("lang", ["en", "fa", "ru", "zh", "pt", "uz", "id", "es", "fr", "it"])
    def test_panel_renders_in_every_locale_without_a_missing_key(self, lang):
        ui = _bot_ui(lang)
        rule = _rule(id="rule_brand_1234")
        branding.write_config(rule, {"triggers_enabled": True, "origin_ids": ["-10055"]})
        text, kbd = ui._render_smart_menu(rule)

        assert "ui_smart_brand" not in text, "a catalog key leaked into the panel"
        assert "ui_brand_" not in text
        labels = [b.text for row in kbd.inline_keyboard for b in row]
        assert all(label and not label.startswith("ui_") for label in labels)
        # The panel is rendered from the catalog, not hardcoded English.
        assert len(_menu_callbacks(kbd)) >= 8
