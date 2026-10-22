# Telegram handler architecture

The bot's control panel is the largest surface in the repository. This document
is its map: what each file owns, how a handler is mounted, and the order in
which the remaining surfaces are being extracted from `pro_bot_ui.py`.

## Layers

| Layer | Path | Owns |
| --- | --- | --- |
| Domain | `core/domain/` | Entities, value objects, pure policy (`entities.py`, `text_replace.py`, `rule_settings.py`) |
| Application | `core/application/` | Use cases over repository ports (`use_cases.py`), command table (`command_registry.py`), discovery policy (`discovery.py`) |
| Infrastructure | `core/infrastructure/` | Persistence, sessions, gRPC, and the Telegram surfaces |
| Composition | `core/main.py` | `build_container()` wires repositories into use cases into `ProBotUI` (constructor injection; no service locator) |

A handler module may use the domain and application layers. It must not import
persistence — `core/tests/test_handler_architecture.py` enforces that, and the
`handlers/` package is scanned for it.

## The handler registry

`core/infrastructure/telegram/handlers/registry.py` is the single inventory of
mounted surfaces. A module declares:

* `name` — its identity in errors and tests;
* `commands` — the slash commands it serves, validated against
  `core.application.command_registry` at construction. A typo, an alias, a
  command another surface owns, or a `builtin` command already answered by a
  dedicated handler fails at import time;
* `register` — the method on the panel that mounts its callback handlers.

`HandlerRegistry.mount(bot, ui)` then checks the promise against the object
being mounted: the mixin must be in the MRO, every declared command must resolve
to a `_cmd_<name>` method, and no command may be claimed twice. Registration
order is the order of `HANDLER_MODULES` in `handlers/__init__.py`.

Slash commands reach their module through
`core/infrastructure/telegram/command_router.py`: one filter over
`ROUTED_TOKENS` at group `-2`, dispatched by name to `_cmd_<name>`. Aliases
(`/guide`, `/lang`) resolve to the canonical command. A command is therefore
owned by exactly one method — the property the registry and
`test_handler_architecture.py` both hold.

## Surfaces

| Module | Owns | Registered by |
| --- | --- | --- |
| `handlers/discovery.py` | `/getchannel`, `/getgroup`, `/getuser`, `/getbot` (with `-id` for the copy-ready one-line list, `-all` for every kind, an optional page number), the chat catalog render and its callbacks | text-command router (group `-2`) |
| `handlers/forwarding.py` | `/forward`, `/rules`, the task list and (next) the forwarding status screen | `register_forwarding_handlers` (group `0`) |
| `command_router.py` (`TextCommandMixin`) | Filters, discovery lists, account commands, `/replace`, clone, guide | text-command router |
| `autoreply_bot_ui.py` | v28 auto-reply panel | `register_autoreply_handlers` |
| `replace_bot_ui.py` | replace manager (list, detail, editor, test) | `register_replace_handlers` |
| `fsm_arbiter.py` | group `-3` settlement of every command token against an armed step | `mount` on `BotManager` |
| `bot_manager.py` | `_awaiting_*` settlement, free-text guards | `BotManager` |

## Support modules (not handlers)

`ui_state.py` + `ui_state_manager.py` (durable FSM state), `client_pool.py`
(MTProto clients), `dialog_gateway.py` (dialog scan), `durable_queue.py` +
`failure_policy.py` (delivery), `i18n.py` + `locales/` (catalogs), `ui_styles.py`
+ `button_style.py` (presentation), `identity_redaction.py` (log redaction).

## Extraction roadmap

`pro_bot_ui.py` still holds most screens. The order below follows dependencies,
one surface per PR, each with its own regression tests:

1. **discovery** — done (this slice): commands, catalog render, dialog gateway,
   application policy.
2. **forwarding** — task list, per-task controls, forwarding status screen.
3. **rule wizard** — `_render_chat_picker`, `_render_rule_detail`,
   `_render_advanced_settings`, the `_step_rule_*` handlers.
4. **addons** — cleaner, filters, header/footer, watermark, every-N, smart mode.
5. **account** — sessions, credentials, login, language, upgrade, dashboard.
6. **admin** — admin panel, broadcast, diagnostics, dead-letter queue.
7. **ai** — AI config, prompts, drafts, rewrite menus.

Each step: move the file, declare it in `HANDLER_MODULES` (when it owns
commands), keep the callbacks' regex ids stable, and add the surface to
`core/tests/bot_dispatch.py::REGISTERING_MODULES` so the callback harness sees
its `filters` import.
