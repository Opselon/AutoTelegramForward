# Branding hop, AI rewrite composition, and the replace maps

The three features that decide *what a forwarded message says and who it
says it is from*, and how they compose. Everything here is one shared
decision: the Python engine, the Go worker and the preview panels all call
the same logic, and the cross-engine parity fixtures
(`worker/internal/parity/branding_parity_test.go`) fail the build when the
two engines drift.

---

## 1. The branding hop (A → C → B) and the channel-only gate

A task can deliver its matched messages **through** one of the operator's
own channels (C) before the final target (B) sees them: the message is
posted into C first and only then forwarded from C to B, which makes B's
forward header name C instead of the original source. The enable switch and
the channel are the rule's first-class fields (`use_intermediate`,
`intermediate_channel_id`); the trigger policy lives in
`metadata["branding_hop"]`.

### The two delivery shapes

| Incoming message in A | What B receives | Why |
| --- | --- | --- |
| Written by a **normal user** (with `require_channel` on) | A direct native forward — header "Forwarded from \<that user\>" | The gate refuses the hop for non-channel content, so people keep their own name |
| **Channel content** (a native channel post in A, or a forward whose origin is a channel) | A → C → B: header "Forwarded from \<C\>" | The hop runs, C looks like the publisher |

Turn the gate on with `branding_require_channel` (registry setting, group
`branding`). It composes with `branding_require_forward`: together they read
"only content that arrived as a channel forward".

The reason codes are user-visible (the bot's reason panel localizes them):
`branding_requires_channel_origin` / `branding_requires_forward_origin` —
see `core/infrastructure/telegram/reason_text.py`.

### Trigger policy (all optional; nothing configured = always hop)

* `branding_origin_titles` / `branding_origin_ids` — match the forward-origin
  channel (Telegram's "Forwarded from" header).
* `branding_keywords` — match the message text (Persian/Arabic fold applied).
* `branding_match_mode` — `any` / `all`.
* `branding_require_forward` / `branding_require_channel` — gates, not checks.
* `branding_force_forward` — stage 2 is a native forward of C (the only way B
  shows C in the header).

### Surfaces

* **Bot**: smart menu — the branding block shows the state and the
  "📡 Channel-only hop" toggle joins the forward-gate and force-forward
  toggles (`rsmrc:`).
* **Web**: `RuleModal` → Branding hop section (checkbox + trigger fields +
  plain-language route summary).
* **Mobile**: rule editor (enable + channel C) and the settings sheet's
  `branding` group (triggers, gates).
* **REST**: `GET/PUT /rules/{id}/settings` — the `branding` group.

### Failure trail

Hop failures are structured events, not stdout lines:

| Event code | Emitted when | Where |
| --- | --- | --- |
| `BRANDING_HOP_FAILED` | the Python hop post into C raised | `pipeline._perform_brand_hop` → audit `forward.branding_failed` |
| `BRANDING_HOP_EMPTY` | the hop posted nothing into C | same path |
| `BRANDING_STAGE1_FAILED` | the Go worker's post into C failed (ambiguous outcomes mark the job UNKNOWN) | `worker/internal/engine/delivery` |
| `BRANDING_STAGE2_FAILED` | C → B failed after C was confirmed | dispatch's branding path |

All four reach the Rust log store (`service="worker"|"core"`,
`category="forward"|"delivery"`) and filter cleanly in the Log Center.

---

## 2. AI rewrite composition (the "3 branding channels" scenario)

The transform stage order — one place in each engine — is:

```
cleaner → link engine → emoji → content policy
       → AI rewrite (prompt from the template library, config from ai_configs)
       → final replacements (task replace rules, mention map)
       → header/footer → advanced transforms
```

The branding hop posts the **post-transform** text into C, and B receives it
without re-processing. So when AI rewrite is enabled on a branded task:

1. polling receives the message from A,
2. the engine rewrites the text (template from `prompt_id`, config from
   `ai_config_id`, per-target prompt overrides honoured),
3. the **rewritten** text is posted into C,
4. C's post is forwarded to B — the rewrite lands in B exactly once.

That holds for any number of branded tasks: each task owns its own C, its
own AI config and its own prompt, so three branding channels are three
independent rewrite-then-hop routes. The Go worker re-decides the hop at
claim time from the live rule and never re-applies a transform the payload
marks as done (`transforms_version`).

### The prompt library and enable/disable

Templates are owner-scoped rows in `prompt_templates` (versioned, OCC-guarded;
system templates shared read-only). The **enable switch** is the `enabled`
column:

* a disabled template stays in the library — preview and the tester keep
  working (that is how an operator checks a prompt before re-enabling it);
* the delivery path treats it as not set: the task is rewritten with its AI
  config's own prompt (`[PIPELINE:AI_REWRITE:PROMPT_OFF]` in Python; the Go
  store resolves a disabled template as not found);
* enabling again restores the previous behavior with nothing re-entered.

Surfaces: bot `AI → 🧠 Prompts` (list, create, rename, edit system prompt /
user template / description, test, delete, enable/disable), the dashboard's
Prompt Editor, and the mobile AI screen's prompt section (CRUD + test over
`/api/prompts`).

---

## 3. The replace maps ("replace any link / any @ID")

Two registry settings (group `cleanup`) + one column-backed map, applied in
the final replacement pass **after** the AI stage:

* `link_replace_enabled` — the toggle. With it on and a map configured, a
  task with no stricter link policy upgrades to `REWRITE_LINKS`.
* `link_rewrite_map` — three key shapes:
  * `"*": "https://t.me/me"` — rewrite **every** link to one target;
  * `"olddomain.com": "newdomain.com"` — rewrite one domain (and subdomains);
  * `"https://old.t.me/x": "https://new.t.me/y"` — rewrite the URLs it
    prefixes (deep-link redirect).
* `mention_rewrite_map` — `{"@oldshop": "@newshop"}` rewrites one handle
  (case-insensitive); `"*": "@main"` rewrites every @ID. Exact handles win
  over the catch-all; one pass, so output is never re-matched.

Mentions are deliberately *not* part of the link engine (`a mention is not a
link` — stripping them was a cleaner concern); they have their own pass so
removal and rewriting can never disagree.

Surfaces: the bot's smart menu → **🔁 Link & @replace maps** (`rmap:` panel:
toggle + one-pair-per-line editors, `-` clears), the dashboard's RuleModal
section (checkbox + two textareas), the mobile settings sheet's registry
rows, and `GET/PUT /rules/{id}/settings` for everything else.

---

## 4. Go worker configurability

The worker is a port, not a parallel feature: the trigger policy above is
consumed by `worker/internal/domain/branding` (parity-tested against the
Python reference), the replace maps by `worker/internal/domain/cleaning`
(`LinkReplaceEnabled`, `LinkRewriteMap`, `MentionRewriteMap` projected from
the same metadata the Python engine reads, mirrored key-for-key in
`internal/domain/settings/specs.go` — the registry-parity test fails if the
two key sets drift). A new branding knob therefore lands in four places:
the Python registry, the Go registry mirror, the rulestore projection and
the engine stage — and the existing tests name each one.
