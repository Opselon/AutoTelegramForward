# Fixture harness — shared schema and routing contract

**Scope of this file.** Every fixture harness (Python, Go, schema validator) must obey
this contract so the three runners cannot disagree about what a fixture means.
Coordinator-only source of truth for Phase C.

## Fixture record shape (`docs/behavior-audit/fixtures.json`)

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | stable fixture id, unique across the file |
| `engine` | yes | the production engine under test (free text, names a real module) |
| `group` | yes | fixture group used by the coverage report |
| `description` | yes | one line, what behavior is pinned |
| `input` | yes | engine input; shape is engine-specific |
| `expected_python` / `expected_python_output` | per class | expected Python result |
| `expected_go` / `expected_go_output` | per class | expected Go result, only when Go has the engine |
| `expected_filter_engine` / `expected_link_block_reason` etc. | per class | multi-engine fixtures may name each expectation separately |
| `notes` | no | evidence and divergence explanation |
| `executable` | yes | routing class, one of the values below |

## `executable` routing classes (exact tokens)

| Token | Meaning | Harness behavior |
|---|---|---|
| `python` | a real Python production engine executes it | `test_fixtures_python.py` must run it |
| `both` | Python and Go both have the engine and the expectation is comparable | both harnesses run it; the coverage report records the comparison |
| `meta` | a numeric/counting claim (length units) executed by a small probe | Python harness executes the probe; no Go equivalent asserted |
| `schema` | characterizes system-level behavior (queue, scheduler, dedup store) that needs infrastructure | **not executed**; classified as characterization data, never reported as a passing test |
| `unclassified` | harness could not map the engine | **hard failure** — a new fixture must declare a class |

Rules:
- A `schema` fixture is never reported as a passing test. It is coverage data.
- An absent Go expectation on a `python` fixture is not a divergence; it is
  "no Go engine". Only `both` fixtures produce parity verdicts.
- `expected_go_*` keys on a `python` fixture are ignored by the Go harness and
  must not be treated as a failed parity check.

## Fixture integrity gates (enforced by the schema validator, runs in CI)

1. JSON parses and `schema_version` is present.
2. `id` unique across the file.
3. Every record has all required fields and a known `executable` token.
4. `input` is a dict (the harness never accepts a bare string).
5. At least one expected-result key is present (`expected_python*` or,
   for `both`, both language expectations).
6. No stray placeholder keys (any `-*` key is rejected — an earlier draft
   carried `-expected_python_output`).
7. Divergence fixtures (`both` with differing expectations) must carry a
   `notes` field citing the finding id.

## Harness rules

- Harnesses load `docs/behavior-audit/fixtures.json` relative to the repo root,
  found by walking up from the test file. Never a hard-coded absolute path.
- Harnesses import **production** domain modules. No reimplementation of
  FilterRule/ContentPolicy/Cleaner logic inside the tests.
- Assertion failures must print the fixture id, the engine, expected and actual.
- A fixture with a wrong expectation is fixed in the JSON with a recorded
  reason in `notes` — never by weakening the expectation silently.
- No network, no database, no sleeps, no Telegram.
