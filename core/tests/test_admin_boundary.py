"""The admin boundary, pressed as a user would press it.

`_is_admin` is the install-wide privilege gate: the admin panel body counts
every tenant's users, every active task, the dead-letter queue and the
forced-subscription channel, while the admin callbacks suspend users, broadcast
to everybody, and add administrators. These drive the *registered* handlers
through `BotHarness.press` — the same regex the dispatcher would match, with the
`from_user` the test chooses — because a gate that lives in a private helper but
not in the handler proves nothing about the user's boundary.

The findings:

* the broadcast **cancel** button (`admbcx`) had no `_is_admin` gate. It cleared
  the *caller's* own UI state and then rendered the admin panel, so a non-admin
  got the install-wide numbers for the price of one click;
* `_render_admin_panel` itself trusted its caller. Every gated callback that
  used it was fine, but the renderer is where all those numbers are actually
  assembled, so it now fails closed there too: a future handler that forgets the
  gate cannot leak the panel by forgetting one call.

The tests also pin the other half — that the gate is *visible* (a refusal the
user can see, never silence) and that an admin's own press still works.
"""
from __future__ import annotations

import pytest

from core.infrastructure.telegram.pro_bot_ui import CB
from core.tests.bot_dispatch import BotHarness

ADMIN = 5094837833
TENANT_A = 5989354154
TENANT_B = 6278059256


@pytest.fixture()
def harness(tmp_path):
    h = BotHarness(str(tmp_path / "admin.db"), admin_ids=[ADMIN])
    yield h
    h.close()


#: The three refusals the admin surface uses. All are honest answers to the user
#: ("you may not", "admin only", "owner only"); the test accepts any, and
#: requires one — silence is the failure mode it is looking for.
REFUSAL_KEYS = ("ui_rule_not_found_or_not_owner", "ui_need_admin", "ui_owner_only_alert")


def refusal_texts(harness_) -> list[str]:
    """The refusal texts, resolved from the same keys the UI uses."""
    return [harness_.ui._t(key, lang="en") for key in REFUSAL_KEYS]


def refused(harness_, cq) -> bool:
    texts = refusal_texts(harness_)
    return any(any(denial in answered for denial in texts) for answered in cq.answers.texts)


def _panel_body_in(text: str) -> bool:
    """The panel body is the only place the install-wide numbers appear."""
    return "total_users" in text or "ui_admin_panel_body" in text


@pytest.mark.asyncio
async def test_a_non_admin_pressing_admin_panel_is_refused(harness):
    """A tenant sees a refusal, not the install numbers."""
    cq = await harness.press(CB["admin"], TENANT_A)
    assert cq.answers, "the press produced no visible answer at all"
    assert refused(harness, cq), cq.answers.texts
    assert not any(_panel_body_in(t) for t in cq.answers.texts), (
        "the admin panel was rendered for a non-admin"
    )


@pytest.mark.asyncio
async def test_the_admin_panel_renders_for_an_admin(harness):
    """The refusal is a boundary, not a breakage: the admin still gets in."""
    cq = await harness.press(CB["admin"], ADMIN)
    assert not refused(harness, cq), "an admin was refused their own panel"


@pytest.mark.asyncio
async def test_the_broadcast_cancel_button_does_not_open_the_panel(harness):
    """`admbcx` had no gate of its own: it cleared state and rendered the panel.

    The fix puts the gate on the button *and* on the renderer, so either half
    going missing fails here.
    """
    cq = await harness.press(CB["adm_bcast_cancel"], TENANT_A)
    assert refused(harness, cq), (
        "a non-admin press of the broadcast cancel button was not refused"
    )
    assert not any(_panel_body_in(t) for t in cq.answers.texts), (
        "the admin panel body leaked through the cancel button"
    )


@pytest.mark.asyncio
async def test_every_admin_callback_refuses_a_non_admin(harness):
    """Every registered admin-only callback must visibly refuse a tenant.

    A handler that returns silently is also a finding: the user cannot tell a
    dead button from a refusal.
    """
    admin_payloads = [
        CB[k] for k in (
            "adm_stats", "adm_users", "adm_bcast", "adm_bcast_confirm",
            "adm_bcast_cancel", "adm_forcesub", "adm_user_view", "adm_admins",
            "adm_admin_add", "adm_config",
        )
    ] + [
        f"{CB['adm_user_view']}_{TENANT_B}",
        f"{CB['adm_user_suspend']}_{TENANT_B}",
        f"{CB['adm_user_activate']}_{TENANT_B}",
        f"{CB['adm_admin_del']}_{TENANT_B}",
        f"{CB['adm_user_plan']}_{TENANT_B}",
    ]
    pressed = 0
    for payload in admin_payloads:
        if not harness.handlers_for(payload):
            # No handler claims it (a truncated payload is not a button), so
            # there is nothing to refuse — and nothing to leak.
            continue
        pressed += 1
        cq = await harness.press(payload, TENANT_A)
        assert refused(harness, cq), (
            f"admin callback {payload!r} did not visibly refuse a non-admin "
            f"(answers={cq.answers.texts})"
        )
    assert pressed >= 10, f"only {pressed} admin callbacks were actually pressed"


@pytest.mark.asyncio
async def test_suspend_and_broadcast_are_not_reachable_by_a_tenant(harness):
    """The destructive admin actions: no state change, no queue row."""
    await harness.users_repo.get_or_create(TENANT_B)

    cq = await harness.press(f"{CB['adm_user_suspend']}_{TENANT_B}", TENANT_A)
    assert refused(harness, cq), cq.answers.texts
    row = harness.db.query_one("SELECT status FROM users WHERE user_id=?", (TENANT_B,))
    assert not (row and row["status"] == "suspended"), "a tenant suspended another user"

    cq = await harness.press(CB["adm_bcast_confirm"], TENANT_A)
    assert refused(harness, cq), cq.answers.texts
    queued = harness.db.query_one("SELECT COUNT(*) AS n FROM broadcast_queue")
    assert int(queued["n"] if queued else 0) == 0, "a tenant queued an install-wide broadcast"


@pytest.mark.asyncio
async def test_an_admin_can_still_admin(harness):
    """The boundary must not make the panel unusable for the admin."""
    await harness.users_repo.get_or_create(TENANT_B)

    cq = await harness.press(f"{CB['adm_user_suspend']}_{TENANT_B}", ADMIN)
    assert not refused(harness, cq), "the admin's own suspend action was refused"
    row = harness.db.query_one("SELECT status FROM users WHERE user_id=?", (TENANT_B,))
    assert row and row["status"] == "suspended", "the admin action did not take effect"
