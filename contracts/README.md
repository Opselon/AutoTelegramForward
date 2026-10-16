# API contract fixtures

Three clients read the same server: the Telegram bot's Go API, the web dashboard
(`web/frontend`), and the Flutter app (`mobile/`). The failure they all share is
silent — one side renames a JSON field, everything still compiles, and a panel
renders blank or a default forever.

These fixtures close that gap. They are **verbatim responses captured from a
running `atf-api`**, and they are the reference for:

- `scripts/audit_api_contract.py` — asserts the Go producer and the Dart and
  TypeScript consumers all describe the same fields (CI: *API contract audit*);
- `mobile/test/core/system_contract_test.dart` — parses each fixture with the
  shipping mobile models;
- the Go handler tests and the web types, which are reviewed against them.

## Files

| Fixture | Endpoint | Shape |
| --- | --- | --- |
| `system_setup.json` | `GET /api/system/setup` | readiness verdict, version block, env contract, live service probes, feature list, next steps |
| `system_env.json` | `GET /api/system/env` | the env contract alone (booleans, never values) |
| `health_services.json` | `GET /api/v1/health/services` | per-service online state |

## Rules

1. **Shape only, never values.** The contract is booleans by construction: a
   check reports *whether* a key is configured (`set`), never its value. The
   capture script refuses to write a fixture containing a credential-shaped
   string or a `value`-style field.
2. **Re-capture from a live server**, don't hand-edit. Payload changes come from
   the handler, not from the fixture:

   ```bash
   python scripts/capture_contract_fixtures.py --base http://127.0.0.1:8080
   ```

3. **Change all three ends in one commit.** A payload change lands with the Go
   handler, the Dart model, the TypeScript type and the refreshed fixture; the
   audit then proves they agree before the tests run.

## What the audit catches

- a field the API sends that a client does not read → blank value in the UI;
- a field a client reads that the API never sends → a typo frozen into a default;
- a client reading a value-bearing field out of a contract payload → a possible
  secret on screen;
- drift between `/api/system/setup` and `/api/v1/health/services` for the
  `services[]` shape, which would make one screen render blanks.

Optional-at-runtime fields (`hint`, `commit_sha`) are warnings, not errors: the
server omits them when empty.
