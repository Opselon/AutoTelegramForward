# Bot text inventory (file by file)

Generated from `scripts/i18n_audit.py` (`stats.modules`); re-run the audit and
regenerate this table after a change to the Telegram surface.

* **handlers** — update handlers the module registers.
* **locale-bound** — the render language is isolated, either by
  `@self._localized_handler` or by an explicit `lang=` on every translation call
  in that handler.
* **no-context calls** — `_t()` calls that pass no language and rely on the
  request-scoped `ContextVar`. Inside a wrapped handler that is correct; outside
  one it is the process default, which is where the four defects fixed in this PR
  lived.
* **visible calls** — `reply_text` / `send_message` / `answer` / keyboard
  constructors and button-row helpers.
* **keys** — distinct catalog keys the module references.

| module | lines | handlers | locale-bound | `_t` calls | no-context calls | visible calls | keys |
|---|---:|---:|---:|---:|---:|---:|---:|
| `core/infrastructure/telegram/__init__.py` | 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| `core/infrastructure/telegram/album_aggregator.py` | 148 | 0 | 0 | 0 | 0 | 0 | 0 |
| `core/infrastructure/telegram/auth_state.py` | 475 | 0 | 0 | 0 | 0 | 0 | 0 |
| `core/infrastructure/telegram/bot_manager.py` | 564 | 11 | 11 | 38 | 0 | 32 | 27 |
| `core/infrastructure/telegram/bot_transport.py` | 514 | 0 | 0 | 0 | 0 | 3 | 0 |
| `core/infrastructure/telegram/button_style.py` | 193 | 0 | 0 | 0 | 0 | 2 | 0 |
| `core/infrastructure/telegram/channel_catchup.py` | 259 | 0 | 0 | 0 | 0 | 0 | 0 |
| `core/infrastructure/telegram/channel_polling.py` | 462 | 0 | 0 | 0 | 0 | 0 | 0 |
| `core/infrastructure/telegram/client_pool.py` | 631 | 0 | 0 | 0 | 0 | 0 | 0 |
| `core/infrastructure/telegram/diagnostics.py` | 530 | 0 | 0 | 22 | 22 | 0 | 18 |
| `core/infrastructure/telegram/dispatcher.py` | 960 | 0 | 0 | 0 | 0 | 13 | 0 |
| `core/infrastructure/telegram/durable_queue.py` | 1704 | 0 | 0 | 0 | 0 | 17 | 0 |
| `core/infrastructure/telegram/errors.py` | 573 | 0 | 0 | 0 | 0 | 0 | 0 |
| `core/infrastructure/telegram/failure_policy.py` | 199 | 0 | 0 | 0 | 0 | 0 | 0 |
| `core/infrastructure/telegram/identity_redaction.py` | 96 | 0 | 0 | 0 | 0 | 0 | 0 |
| `core/infrastructure/telegram/login_flow.py` | 400 | 0 | 0 | 2 | 2 | 0 | 0 |
| `core/infrastructure/telegram/pro_bot_ui.py` | 11989 | 258 | 258 | 1676 | 835 | 782 | 990 |
| `core/infrastructure/telegram/pv_responder.py` | 272 | 0 | 0 | 0 | 0 | 1 | 0 |
| `core/infrastructure/telegram/raw_tracer.py` | 283 | 0 | 0 | 0 | 0 | 0 | 0 |
| `core/infrastructure/telegram/reason_text.py` | 164 | 0 | 0 | 6 | 6 | 0 | 0 |
| `core/infrastructure/telegram/registry_editor.py` | 730 | 0 | 0 | 89 | 89 | 0 | 41 |
| `core/infrastructure/telegram/route_lists.py` | 295 | 0 | 0 | 0 | 0 | 0 | 0 |
| `core/infrastructure/telegram/session_boot.py` | 229 | 0 | 0 | 0 | 0 | 0 | 0 |
| `core/infrastructure/telegram/sync_engine.py` | 448 | 0 | 0 | 0 | 0 | 3 | 0 |
| `core/infrastructure/telegram/ui_state.py` | 78 | 0 | 0 | 0 | 0 | 0 | 0 |
| `core/infrastructure/telegram/ui_state_manager.py` | 490 | 0 | 0 | 0 | 0 | 0 | 0 |
| `core/infrastructure/telegram/ui_styles.py` | 339 | 0 | 0 | 0 | 0 | 0 | 0 |
| **total (27 modules)** | 23026 | 269 | 269 | 1833 | 954 | 853 | 1076 |
