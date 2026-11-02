# Stage 4.1 — Implementation Plan (FilterRule + ContentPolicy parity)

**Status: PLANNED — NOT IMPLEMENTED.** This document is a design. It touches no
code, no schema, no fixture, and no runtime behavior. It is written to be
reviewed and then executed as a separately tracked set of PRs against master
`35ee530` on branch `audit/complete-business-logic-inventory` (PR #289, head
`c74498d`). Everything here is consistent with
`docs/behavior-audit/FINDINGS.md` and `docs/architecture/migration-readiness.md`.

**Scope of Stage 4.1** (per `migration-readiness.md` §Recommended sequence,
item 1): port `ContentPolicy` evaluation to the Go delivery path, and resolve
the three findings that block that stage from being called complete —
- **4.1a — F-12:** `BlockLinks` exists in the Go engine but is never populated.
- **4.1b — F-14:** three non-equivalent "contains a link" predicates.
- **4.1c — F-11:** the `block_links` gate sits at a different position in the
  two engines' check order.

**Dependency order: 4.1b → 4.1a → 4.1c.** The reasons are given in §7.

---

## 1. The verified starting position

Everything below is source-verified (see
`docs/behavior-audit/settings-persistence-audit.md` §1 for the full chain).

### 1.1 Store topology — there is nothing to project

- **SQLite `filter_rules`** has exactly **14 columns**: `id, name,
  whitelist_keywords, blacklist_keywords, regex_patterns,
  allowed_media_types, blocked_media_types, drop_service_messages,
  min_message_length, max_message_length, owner_user_id, regex_block,
  regex_require, is_enabled`. The table is declared three times in
  `core/infrastructure/persistence/database.py` — the v1 base schema
  (`:54-65`), the v11 rebuild (`:453-468`), and the v30 heal (`:923-938`) —
  and all three agree. **There is no `ALTER TABLE filter_rules` anywhere in
  the file.** The only additive path is `_ADDITIVE_COLUMNS`
  (`:1157-1163`), which adds exactly `owner_user_id`, `regex_block`,
  `regex_require`, `is_enabled`. `MIGRATIONS` runs to v33.
- **PostgreSQL has no `filter_rules` table at all.** `scripts/postgres/
  schema.sql` does not declare it; `postgres_repositories.py` defines no
  filter repository (its classes are `PostgresForwardRuleRepository` `:333`,
  `MessageMap` `:748`, `DeliveryQueue` `:937`, `DeliveryAttempt` `:1547`,
  `IngestionGap` `:1675`, `Prompt` `:1823`, `UserTask` `:2368`).
- **The Go worker reads filters only from SQLite.**
  `worker/internal/rulestore/rulestore.go:9-13` documents this and `:94-106`
  enforces it — "filter_rules is ALWAYS SQLite under the current composition
  root … Creating a PostgreSQL filter_rules table [would duplicate it]; the
  Core serves it from SQLite." `drivers.go:57-67` holds the projection, which
  reads the 9 NOT NULL columns and then picks up `regex_block`,
  `regex_require`, `is_enabled` through NULL-tolerant optional helpers.

**Consequence: 4.1a is not "copy a column into the projection." There is no
column. The Go field `rule.FilterRule.BlockLinks` (`rule.go:101`) is read by
`filter.go:213` and written by nothing — `grep -rn "BlockLinks" worker/ api/`
returns exactly those two lines. It is unconditionally `false`.**

### 1.2 The Python `FilterRule` entity has no `block_links` field either

`core/domain/entities.py:2312-2333` declares the dataclass with 14 fields
matching the 14 columns. `core/domain/services.py:79` reads it via
`getattr(filter_rule, "block_links", False)`, which is `False` for every
entity the repositories can construct, and no constructor in `core/` passes
`block_links=` to `FilterRule(...)`. **The Python filter-side gate is dead
code by construction.** So the F-12 divergence is a three-way modelling
absence, not a projection gap:

| Level | Setting persisted? | Field declared? | Gate evaluated? |
|---|---|---|---|
| Task (`ForwardRule.block_links`) | yes — `forward_rules.metadata` | yes (`entities.py:680-687`) | yes — `media_filter.link_block_reason` ← `pipeline.py:957`; `ContentPolicy.evaluate` (`content_policy.py:840`); `sync_engine.py:245` |
| Filter (Python `FilterRule`) | **no** | **no** (`entities.py:2312`) | no — `services.py:79` reads a field that does not exist |
| Filter (Go `rule.FilterRule`) | **no** | yes (`rule.go:101`) | yes (`filter.go:213`) — but always false |

### 1.3 The three link predicates (F-14), verbatim

| # | Predicate | Source | Shape |
|---|---|---|---|
| 1 | `FilterEngine` gate | `services.py:27-30` (`URL_REGEX`) + 3 substring fallbacks, `services.py:79-81`; Go port `filter.go:50,212-219` | `(https?://[^\s]+|www\.[^\s]+|(?:https?://)?(?:t(?:elegram)?\.(?:me|dog))/[^\s]+|@[A-Za-z0-9_]{3,32})` OR `http://`/`https://`/`t.me/` substring |
| 2 | `has_link()` | `content_policy.py:97-100` (`_RE_LINK`), used by `link_block_reason` (`media_filter.py:94`) and `ContentPolicy.evaluate` (`content_policy.py:840`) | `(?:https?://|ftp://|www\.|t\.me/|telegram\.me/|telegram\.dog/|tg://)` |
| 3 | `LinkManagementEngine` | `services.py:836+`, `services.py:1050-1061` | policy-specific |

Coordinator-verified divergence (FINDINGS.md F-14): `tg://resolve?domain=x`
and `ftp://files.example/x` **pass** gate 1 and **DROP** under gate 2;
`"email @user hi"` **DROPs** under gate 1 (the `@[A-Za-z0-9_]{3,32}`
alternation — a mention is not a link) and **passes** gate 2.

### 1.4 The check-order divergence (F-11)

Both engines evaluate a `block_links` gate; they do it at different points.

| Position | Python `FilterEngine.evaluate` | Go `Engine.Evaluate` |
|---|---|---|
| 1 | service messages (`services.py:~52`) | service messages (`filter.go:92`) |
| 2 | filter disabled (`:~63`) | `filter_disabled` (`:101`) |
| 3 | `blocked_media_types` incl. `"link"` entry (`:64-68`) | media blocked + link entry (`:106-120`) |
| 4 | `allowed_media_types` (`:70-72`) | media allowed (`:125-134`) |
| 5 | **`block_links` (`:79-81`)** ← here | `min`/`max` length (`:138-142`) |
| 6 | `min`/`max` length (`:84-87`) | blacklist keywords (`:144-147`) |
| 7 | blacklist / whitelist keywords (`:89-98`) | whitelist keywords (`:150-159`) |
| 8 | explicit + legacy regex lists (`:100+`) | explicit + legacy regex (`:163-208`) |
| 9 | — | **`block_links` (`:213-219`)** ← here |

F-11 is verified: a message that is both link-bearing and over
`max_message_length` yields `link_blocked` from Python and `too_long` from Go.
Same DROP decision, different reason code — a metrics/audit divergence, not a
message-loss divergence (FINDINGS.md F-11 states this explicitly and this plan
does not escalate it).

---

## 2. 4.1a — make `BlockLinks` reachable

### 2.1 The decision: project the task-level flag, do not grow a new column

The F-12 owner-visible symptom is: *a task with `block_links=true` forwards
link-bearing messages on the Go delivery path.* The flag that produces that
symptom is the **task-level** `metadata["block_links"]`, which is already
persisted on `forward_rules` in both stores and already validated by the
registry (`rule_settings.py:131`, SET-019).

The filter-level reading is a duplicate semantic that exists nowhere in the
persistence chain. **This plan therefore does not add a `block_links` column
to `filter_rules`** — not in SQLite and not in Postgres. Per the handoff
constraint ("do not recommend storing new fields unless runtime/operational
value is proven"), the cheaper and correct path is to project the task flag
the worker already has.

**Consequence:** the Go `FilterRule.BlockLinks` field is *retained* but its
source becomes the task, not the filter row. The dead Python gate at
`services.py:79-81` is removed as dead code (see §2.4), because keeping a gate
that reads a field the entity cannot have is a trap for the next reader.

### 2.2 Files likely to change

| File | Change |
|---|---|
| `worker/internal/rulestore/rulestore.go` | In `GetByID`, read the task's link decision into the returned `FilterRule`: after loading the filter row, set `f.BlockLinks` from the **task-level** flag carried on the `ForwardRule` projection (the rule row's `custom_metadata_json` / settings, the same source the Python `pipeline.py:957` gate reads). Keep the field on the filter struct — it is the engine's input — but document its origin as the task, not the filter row, in the struct comment at `rule.go:91-102`. |
| `worker/internal/domain/rule/rule.go` | Comment correction on `FilterRule.BlockLinks`: "mirrors the *task's* `block_links` setting, projected at load; the persisted filter row has no such column." |
| `worker/internal/engine/filter/filter.go` | No change to the gate itself. If 4.1b lands first, the predicate in `:213-219` swaps to the shared helper (§3.2). |
| `core/domain/services.py` | Remove or gate the dead `services.py:79-81` branch. If the owner wants a filter-level switch in future, that is a new feature with a column — out of scope here. |
| `core/tests/test_filter_engine.py` | The existing `link_blocked` tests at `:104` construct a `FilterRule(blocked_media_types=["link"])`, which exercises the *media-list* path, not the dead field; they stay green. Any test that sets `block_links=` on a `FilterRule` must move to a `ForwardRule` fixture. |
| `docs/behavior-audit/fixtures.json` | **Not owned by this plan.** FLT-004 (the F-12 acceptance fixture named in `migration-readiness.md`) is added by the fixtures owner in the separate fix PR, not here. |

### 2.3 Migrations

**None.** That is the point of §2.1 and the answer to the handoff's "required
migrations" question: Stage 4.1a requires **zero SQLite migrations and zero
Postgres schema changes**.

- SQLite: no `ALTER TABLE filter_rules`; `MIGRATIONS` stays at v33.
- PostgreSQL: no `filter_rules` table is created. The Go store reads filters
  from SQLite only (`rulestore.go:94-106`, comment at `:396`: "filter_rules
  always lives in SQLite; rewrite the PG-style placeholder"), so there is no
  Postgres surface to migrate.
- Backup/restore: `_filter_dict` / restore (`backup_service.py:239-259`,
  `:424-443`) are untouched — the 14-column snapshot shape is unchanged, and
  the task-level flag is already in the rule snapshot via `metadata`.
- gRPC contracts: the `FilterRule` message (`proto/autoforward.proto:371-393`,
  14 fields) and both servo maps (`servicers.py:509-526`, `2207-2260`) are
  unchanged. No proto regeneration, no `_pb2.py` churn.

If, contrary to this plan, a future owner decides a filter-level switch has
proven operational value, the column would be added as a **v34** guarded
additive entry in `_ADDITIVE_COLUMNS` (the established pattern for
`filter_rules`: declare in the v30-style `CREATE TABLE IF NOT EXISTS` for
fresh installs **and** in `_ADDITIVE_COLUMNS` for carried-over databases — see
the `is_enabled` precedent at `database.py:1159-1163` and the v30 comment at
`:913-922`), plus a `filter_rules` table in `scripts/postgres/schema.sql` and a
`PostgresFilterRuleRepository`. That is a larger change than this stage
needs and is explicitly out of scope.

### 2.4 Acceptance tests

| ID | Test | Passes when |
|---|---|---|
| A-1 | Task with `block_links=true`, attached filter, message with `https://a.io` | Go engine returns `drop("link_blocked")`; Python pipeline drops with `link_blocked` at `pipeline.py:957`. **Both paths drop.** |
| A-2 | Task with `block_links=true`, no filter attached | Go returns `allow` at the `no_filter` early return (`filter.go:96`) — matching Python, where no attached filter means only the rule-level gate runs; the *task* gate must still drop. This test pins that the projection is on the **task**, so a missing filter cannot re-enable link blocking. (If the projection were per-filter, A-2 would silently pass links.) |
| A-3 | Task with `block_links=false`, filter with `blocked_media_types=["link"]` | Both engines drop via the media-list gate, reason `link_blocked`. The two paths to the same reason must remain distinct and both must work. |
| A-4 | Existing suite | `core/tests` and `worker/internal/...` stay green; no test constructs `FilterRule(block_links=…)`. |
| A-5 | Round-trip | Save a filter through gRPC `CreateFilter`/`UpdateFilter`, read it back, assert the 14 fields; assert no `block_links` appears in the proto response or the backup snapshot. Pins that 4.1a adds no contract surface. |

### 2.5 Risks and rollback

**Risks**
- *Projection ambiguity* — `rulestore.go` must take the flag from the task
  projection, and the task projection is already loaded in `GetByID` (it
  returns `(*ForwardRule, *FilterRule, error)`). The wiring is local; the risk
  is a future reader assuming the field comes from the filter row and
  "fixing" it back. Mitigation: the `rule.go` comment in §2.2, and A-2.
- *Task-gate duplication* — once the Go filter drops for `block_links` and
  the ingest-side Python decision is baked into the payload
  (`delivery.go:193-199`), the same message is decided twice. That is the
  intended strangler-fig redundancy; the reason strings must agree so an
  operator debugging one drop sees one vocabulary.
- *F-11 interaction* — landing 4.1a before 4.1c makes the Go engine
  drop `link_blocked` where it previously dropped `too_long` for
  link-bearing over-length messages. Order 4.1c with or after 4.1a (§7).

**Rollback** — revert the `rulestore.go` projection. Python remains the ingest
owner (per `migration-readiness.md` §Rollback strategy), so a reverted worker
reverts to today's behavior with no data migration to undo and no schema to
remove. This is the cheapest rollback of any stage in the blueprint.

---

## 3. 4.1b — one canonical "contains a link" predicate

### 3.1 The decision: `_RE_LINK` becomes the single predicate

Gate 2 (`_RE_LINK`, `content_policy.py:97-100`) is the right canonical form:

- It is the predicate the **production path** already uses for the rule-level
  gate (`link_block_reason` ← `pipeline.py:957`) and for `ContentPolicy`
  itself (`content_policy.py:840`) — i.e. it is what `block_links` and
  `block_links`-bearing policies actually mean today.
- It is the **broader** of the two: it covers `ftp://`, `tg://`,
  `telegram.me/`, `telegram.dog/`, which gate 1 misses, and it does not treat
  an `@mention` as a link.
- It is a plain RE2-compatible alternation of literal prefixes — trivially
  portable to Go with no Unicode property classes, which matters because the
  F-03 emoji problem (Go RE2 cannot express the Python pattern) does **not**
  apply here.

Gate 1's `@[A-Za-z0-9_]{3,32}` alternation is a bug (F-14: a mention is not a
link), and its substring fallbacks are subsumed by the prefix alternation.

**Chosen predicate:** `(?:https?://|ftp://|www\.|t\.me/|telegram\.me/|telegram\.dog/|tg://)`
(case-insensitive). This changes gate 1's behavior on `ftp://`, `tg://` and
`@mentions` — the coordinator-verified F-14 table — in the direction the
production path already takes.

### 3.2 Files likely to change

| File | Change |
|---|---|
| `core/domain/services.py` | `URL_REGEX` at `:27-30` loses the `@mention` alternation **only for gate purposes**. Care: `URL_REGEX` is also the *strip* pattern family (`remove_urls` at `:160-170` has its own sub-patterns and does NOT use `URL_REGEX` — FINDINGS F-13). The gate at `:79-81` swaps to `has_link()` from `content_policy.py`; the three substring fallbacks are deleted. Verify every `URL_REGEX` call site and split gate use from strip use before editing (F-13 cross-check). |
| `core/domain/content_policy.py` | `has_link` exported as the canonical predicate; `_RE_LINK` unchanged. |
| `worker/internal/engine/filter/filter.go` | `urlRegex` (`:50`) is replaced for **gate** purposes by a port of `_RE_LINK`; the substring fallbacks at `:214-216` are deleted. `transform.go:26`'s `urlRegex` is a different use (strip — F-13) and must NOT be aliased to the gate pattern. |
| `core/domain/content_policy.py` (`ContentPolicy.evaluate`) | Already uses `has_link`; unchanged. |
| fixtures | LNK-GATE-01/02 (`migration-readiness.md` backlog P0) get the canonical expectation — owned by the fixtures owner, not this plan. |

### 3.3 Migrations

**None.** 4.1b is pure logic: no schema, no proto, no contract field, no
backup shape. The predicate is internal to both engines.

### 3.4 Acceptance tests

| ID | Input | Expected (both engines) |
|---|---|---|
| B-1 | `see tg://resolve?domain=x` | DROP (gate was pass) |
| B-2 | `ftp://files.example/x` | DROP (was pass) |
| B-3 | `email @user hi` | **pass** (was DROP — a mention is not a link) |
| B-4 | `visit t.me/foo` | DROP (unchanged) |
| B-5 | `see https://a.io` | DROP (unchanged) |
| B-6 | `no link here at all` | pass (unchanged) |
| B-7 | `www.example.com` / `telegram.dog/x` | DROP (gate-1 `www.` branch already matched; now matched by the prefix alternation instead — pins one code path, not two) |
| B-8 | strip-path regression | `FilterEngine.remove_urls` still strips `[text](url)` keeping the anchor text and still strips raw URLs; `transform.ApplyTargetOverride` still strips **usernames** (its own documented behavior, F-13 — a divergence this plan does NOT fold in, to keep 4.1b reviewable) |

B-1..B-7 are the F-14 coordinator table turned into assertions, and **the
reason code must be identical from both engines** (`link_blocked`).

### 3.5 Risks and rollback

**Risks**
- *Gate/strip conflation* — the single largest risk in 4.1b. `URL_REGEX`
  serves a gate in `services.py:79` and a strip elsewhere; editing the shared
  pattern instead of the gate would silently change link **removal** output
  for every task. Mitigation: B-8, and the F-13 cross-check named in
  `migration-readiness.md`.
- *Reason-code drift under unification* — the two engines' `link_blocked`
  strings must stay byte-identical (`filter.go:53` pins Go reasons to Python's
  vocabulary; keep it).
- *Behavior change is visible* — B-1/B-2 make previously-passing messages
  drop. That is the fix, but an owner relying on `tg://` passthrough sees a
  change. Mitigation: ship in the same release note as 4.1a, since both are
  "the link gate now means what the Python pipeline already meant."

**Rollback** — revert the two predicate edits. No data involved. Note that
rolling back 4.1b after 4.1a re-opens the F-14 two-sided divergence while
leaving F-12 closed; the two are independently revertible but semantically
paired, which is why they ship together.

---

## 4. 4.1c — align the evaluation order

### 4.1 Decision: move the Go gate to the Python position

Adopt the **Python position** (5th, immediately after the media allow/deny
gates and before min/max length) in the Go engine. Rationale:

- Python is today's authoritative ingest path (`migration-readiness.md`), and
  the gate being aligned is the `FilterEngine` gate, whose Python twin sits at
  position 5; aligning like-with-like is the only defensible rule.

**A verified wrinkle:** `ContentPolicy.evaluate` checks length **before**
links (`content_policy.py:835-838` then `:840-842`) — so the *policy* order is
the Go order, not the Python `FilterEngine` order. The choice between the two
is therefore genuinely arational and must be made explicitly. **This plan
adopts the Python `FilterEngine` order** for the reason above: the
ContentPolicy chain is a *separate* evaluation that Stage 4.1's later part
ports wholesale, and its internal order travels with it. Mixing the
`FilterEngine` gate with the ContentPolicy order would create a third hybrid.
The decision is recorded here so a reviewer can overturn it: if the owner
prefers the policy order, 4.1c moves the **Python** gate instead and the Go
engine stays put. Either way the two agree — that is the acceptance criterion,
not the direction.

### 4.2 Files likely to change

| File | Change |
|---|---|
| `worker/internal/engine/filter/filter.go` | Move the `f.BlockLinks` block from `:212-219` (position 9, after regex lists) to immediately after the `allowed_media_types` check (`:125-134`), before `min`/`max` length at `:138`. Update the package doc's ordered list at `:78-81`, which explicitly says "do not reorder, the order *is* the semantics" — that comment must be edited in the same commit or it contradicts the code. |
| `core/domain/services.py` | No change if the Go move is chosen (Python is already at position 5). |
| `core/tests/test_filter_engine.py`, `worker/internal/engine/filter/*_test.go` | Any test that asserts `too_long` for a link-bearing over-length message flips to `link_blocked`. Both engines must then agree. |

### 4.3 Migrations

**None.** Order-only change; no schema, contract, or persistence involvement.

### 4.4 Acceptance tests

| ID | Fixture | Expected (both engines, identical reason) |
|---|---|---|
| C-1 | `block_links=true`, `max_message_length=50`, text 100 chars with `https://a.io` | DROP, reason **`link_blocked`** (Go previously `too_long`) |
| C-2 | Same, no link in a 100-char text | DROP, reason `too_long` (both, unchanged) |
| C-3 | `block_links=false`, over-length | DROP `too_long` (both, unchanged) |
| C-4 | Over-length AND a blacklist keyword AND `block_links=true` | DROP `link_blocked` — pins that the earlier gate decides first in **both** engines |
| C-5 | Reason-code parity sweep | For a fixed fixture set, Python and Go reason strings are equal element-for-element |

C-1 is the F-11 executed case. C-5 is the operative test of 4.1c: the point is
that an operator reading drop-reason dashboards cannot tell which engine ran.

### 4.5 Risks and rollback

**Risks**
- *Dashboard/alerting shift* — any metric or alert keyed on `link_blocked`
  vs `too_long` changes ratio. Called out in FINDINGS.md F-11; coordinate with
  whoever owns the metrics labels.
- *The `filter.go` package doc* says the order is the semantics and is
  bytecode-pinned by the port's own claim. Editing the code without the
  comment is a documentation lie; editing both is the whole change.
- *Test churn is real but bounded* — only tests spanning the length/link pair
  flip.

**Rollback** — revert the move. No data involved.

---

## 5. Compatibility matrix

Read this as "does the thing on the left need to change for stage 4.1?" All
three sub-stages are included; **all entries are planned, none are done.**

| Surface | 4.1a (BlockLinks) | 4.1b (predicate) | 4.1c (order) |
|---|---|---|---|
| SQLite schema (`database.py`, v1–33) | **no change** — no `block_links` column exists or is added; `_ADDITIVE_COLUMNS` untouched | none | none |
| PostgreSQL schema (`scripts/postgres/schema.sql`) | **no change** — no `filter_rules` table exists or is created | none | none |
| Postgres repos (`postgres_repositories.py`) | **no change** — no filter repository exists | none | none |
| Go store (`worker/internal/rulestore/`) | **change** — project the task flag into `FilterRule.BlockLinks` at load | none | none |
| Go engine (`worker/internal/engine/filter/`) | none to the gate | **change** — swap `urlRegex` gate for the `_RE_LINK` port; drop substring fallbacks | **change** — move the gate to position 5 |
| Go rule struct (`worker/internal/domain/rule/rule.go`) | comment only | none | none |
| Python `FilterEngine` (`core/domain/services.py`) | **change** — delete the dead `:79-81` gate | **change** — gate uses `has_link()`; `URL_REGEX` loses the `@` alternation for gate use only | none (Python is the reference) |
| Python `ContentPolicy` (`content_policy.py`) | none — already correct | none — already canonical | none |
| Proto (`proto/autoforward.proto`) | **no change** — `FilterRule` stays 14 fields, no `block_links` | none | none |
| Generated pb2 / `api/proto/*.pb.go` | **no regeneration** | none | none |
| gRPC servicers (`servicers.py`) | **no change** | none | none |
| Go API (`api/internal/handlers/handlers.go`) | **no change** — `block_links` is already carried as a task metadata key (`:491,591`) | none | none |
| Worker delivery (`worker/.../delivery.go`) | none — task flag rides the rule projection | none | none |
| Tests (`core/tests`, `worker/**/*_test.go`) | **change** — A-1..A-5 | **change** — B-1..B-8 | **change** — C-1..C-5 |
| Backup/restore (`backup_service.py`) | **no change** — 14-field snapshot shape unchanged | none | none |
| Settings registry (`rule_settings.py`) | **no change** — SET-019 already covers the task flag | none | none |
| Bot/Web/Mobile UI | **no change** — `block_links` editor writes the task flag today; web/mobile have no `link_policy` editor at all (`web/frontend/src` shows it read-only, `RulesPanel.tsx:239`) | none | none |

**Backward compatibility, defined:**

1. *Database* — a database from any schema version v1…v33 opens unchanged
   before and after Stage 4.1. No migration runs, no column is added, no table
   is created. `SCHEMA_VERSION` stays 33.
2. *Wire* — the gRPC `FilterRule` message, the Go API JSON payloads, and the
   backup document are byte-compatible. A 4.1-era core and a pre-4.1 client
   interoperate exactly as before; a pre-4.1 backup restores into a 4.1 core
   with no key translation.
3. *Runtime* — Python-only deployments see two behavior changes, both
   intentional and both in §3.4/B-1..B-3 (ftp/tg links now block; mentions no
   longer count as links), plus the removal of a gate that could never fire
   (§2.1). No message that Python drops today is allowed after 4.1, except the
   `@mention` case in B-3, which is the F-14 bug being fixed.
4. *Mixed activation* — a 4.1 worker reading a pre-4.1 database works: the
   projection reads the task flag, which has been persisted since v2's
   `metadata` column. There is no flag-missing state to heal.

**Rollout:**

Stage 4.1 ships as **one reviewed release** with the three sub-stages in
dependency order but a single activation, because they are semantically
paired (§3.5). Sequence within the release:

1. 4.1b (predicate) — lands first so the gate 4.1a wires up is already
   canonical; no temporary divergence window between "reachable" and
   "correct".
2. 4.1a (projection) — the F-12 fix; the P0 backlog item in
   `migration-readiness.md`.
3. 4.1c (order) — last, because it is only observable once 4.1a makes the gate
   reachable; landing it before 4.1a would change nothing at all, and landing
   it after 4.1a means a release where reason codes are already stable.

Flag-gating is **not** required: Python remains the ingest owner
(`migration-readiness.md` §Rollback strategy), so the worker's re-check is a
defense-in-depth evaluation of a decision the Python pipeline already made and
baked into the payload (`delivery.go:193-199`). Worker activation to default
stays a separate,Final-stage release. Reverting any sub-stage is a plain
revert — there is no data to migrate back.

---

## 6. Post-implementation ownership

| Surface | Owner after 4.1 | Note |
|---|---|---|
| Link-gate **predicate** (`_RE_LINK` + Go port) | shared Python/Go, one definition | the whole point of 4.1b; a change in one is a change in the other, reviewed together |
| `FilterRule.BlockLinks` semantics | Go store + Python entity jointly | the field's *source* is the task projection; if a future column is added, both sides move together |
| Drop-reason vocabulary | whichever engine runs | already pinned by `filter.go:53`; C-5 keeps it honest |
| `FilterRule` schema (14 columns) | SQLite migrations + `sqlite_repositories.py` | unchanged by this stage |
| ContentPolicy port (rest of 4.1) | Python → Go | 4.1a/b/c are the prerequisite, not the port; the chain, the three whitelist modes (`any`/`all`/`phrase`), and the per-list switches (`blacklist_off`/`whitelist_off`, v30) follow in the same stage's later PRs per `migration-readiness.md` item 1 |

## 7. Dependency order — why 4.1b first

- **4.1b → 4.1a.** 4.1a makes the Go `block_links` gate *reachable*. If it
  lands first, the reachable gate uses the wrong predicate (F-14), so a fix
  that closes F-12 temporarily widens the two-sided divergence: the Python
  rule-level gate drops `tg://` and `ftp://`, the newly-reachable Go gate
  would pass them. Wiring the canonical predicate first avoids shipping a
  reachable-but-wrong gate at all.
- **4.1a → 4.1c.** 4.1c moves a gate that is currently unreachable in Go
  (`BlockLinks` is always false). Moving it first is dead churn; moving it
  after 4.1a means the reason-code change (C-1) lands in the same release the
  gate starts firing, so dashboards shift once, not twice.
- **4.1b and 4.1a are independently revertible but semantically paired** —
  reverting only 4.1b leaves a reachable Go gate that disagrees with the
  Python rule gate on `tg://`, `ftp://` and `@mentions`. Ship them together;
  revert them together.
- No dependency runs the other way: 4.1c does not gate 4.1b, and neither
  depends on ContentPolicy porting work, which proceeds in parallel once
  4.1b's canonical predicate exists (the policy already uses `has_link`).

## 8. Explicit non-goals

- No `filter_rules` column is added, in either store (§2.1).
- No PostgreSQL `filter_rules` table is created (§1.1); the Go store's
  SQLite-only filter read is respected.
- F-03 (emoji pattern), F-04 (`remove_links`/`link_replacement` precedence),
  F-08 (Go `transform` unreferenced), F-15 (header placeholders), F-16
  (`filter_inline_keyboard`), F-17 (bounds), F-18 (Go link-policy surface) and
  F-19 (auto-delete durability) are **out of scope** — they belong to Stages
  4.2/4.4/4.5 per `migration-readiness.md`, and the bound disagreement F-17 in
  particular is a settings-registry fix, not a filter fix.
- `docs/behavior-audit/fixtures.json` and `docs/feature_catalog.json` are not
  touched by this plan; acceptance fixtures are added by their owner.
- **Nothing in this plan is implemented by this document.** Stage 4.1 is
  planned, not shipped.
