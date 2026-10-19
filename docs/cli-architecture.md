# `atfcli` architecture

`atfcli` is the control CLI for the whole AutoTelegramForward stack. It is a
four-layer package with a one-way dependency rule, enforced by
`core/tests/test_atfcli_architecture.py` rather than by convention.

```
atfcli/
  domain/          pure rules and models      no I/O, no argparse, no HTTP
  application/     use cases + ports          may import domain
  infrastructure/  adapters (API, host)       may import application
  commands/        argparse presentation      may import application
  cli.py           parser, context, dispatch
```

The rule: **`infrastructure → application → domain`, never back.**
`domain` and `application` must not import `infrastructure` or `commands`; the
architecture test parses every module's AST and fails the build if they do.

## What each layer is for

| Layer | Owns | Must not know |
|---|---|---|
| `domain` | validators, resource specs, log vocabulary, secret masking, error taxonomy | that the stack is reachable over HTTP, that a command line exists |
| `application` | use cases (`Tasks`, `Logs`, `Sessions`, …), ports as `Protocol`s, `Outcome`/`Warning_` | which adapter implements a port |
| `infrastructure` | `ApiClient` (transport, auth, retries), `HttpGateways` (JSON → domain objects), host adapters (docker, files, subprocesses) | argparse, how a command prints |
| `commands` | subparsers, flags, formatting | which URLs exist, how JSON is shaped |

`application/container.py` is the composition root: ports in, use cases out. A
test can build the same bundle over in-memory fakes, and no command module ever
learns which adapter it got.

### Anti-corruption boundary

API payloads are parsed **once**, in `infrastructure/http/gateways.py`, into
immutable domain objects that keep the original document as `.raw`. `--json`
prints that stored document, so machine-readable output stays byte-for-byte what
the server sent while the human path reads typed fields.

## One rule, three consumers

A validation rule lives in `domain/validators.py` and answers with a *problem
string or `None`*. Three surfaces consume it:

1. the interactive wizard (`ui.ask(validate=...)`),
2. argparse (`type=validators.argparse_type("chat")`),
3. use cases, before any request goes out.

`argparse_type` is the single adapter allowed to raise, so a rule cannot be
phrased differently on different paths. `test_atfcli_architecture.py` asserts the
two phrasings are identical strings.

## The generic resource surfaces

AI configs, prompt templates and filters are the same shape of problem. Their
differences are stated once in `domain/resources.py` (`ResourceSpec`: path,
envelope key, id/label keys, required fields, editable fields, wire casing), and
`commands/_resource.py` registers the same list/show/create/update/delete verbs
for each. A resource is "one spec line plus one registry line".

Two consequences that are easy to get wrong and are now impossible to get wrong
in one place only:

* **Wire names vs flag names.** The API speaks protojson camelCase (`apiKey`);
  the CLI speaks kebab-case (`--api-key`). `ResourceSpec.flag_of()`/`flag_map()`
  derive both from one list.
* **Silently dropped fields.** The Go handlers unmarshal with
  `DiscardUnknown: true`, so a mistyped key is accepted and thrown away. Every
  write echoes the fields back and warns about the ones that vanished; a
  `--dry-run` warns *before* the write, because that is when someone is checking
  their spelling.

## Command surface

| Group | Verbs |
|---|---|
| `rules` | list, show, logs, create, update, delete, enable, disable, toggle, pause, resume, stop, restart, duplicate, path, simulate, settings {schema,get,set,reset}, sources {list,add,remove,set} |
| `ai` | list, show, create, update, delete, enable, disable, providers, check-key |
| `prompts` | list, show, create, update, delete, test, languages |
| `filters` | list, show, create, update, delete, explain |
| `logs` | stats, query, search, tail, export, purge, policy, health, diagnostics |
| `sessions` | list, show, chats, backup, terminate |
| `data` | queue, dlq, backup-export, backup-inspect, backup-import |
| `admin` | overview, users, settings, admins, extensions, broadcast |
| `audit` | events, outcomes |
| `status`, `doctor`, `validate`, `docker`, `db`, `certs`, `net`, `deploy`, `system`, … | host lifecycle and diagnostics |

Identifiers accept a full id, a unique id prefix (≥4 characters) or a name, and
that resolution happens once in the use case, so every verb accepts the same
forms and an ambiguous prefix is refused with the candidates listed.

## Defects this structure fixed

Each of these was a silent wrong answer, not a crash:

* `rules simulate` posted `text`/`sender`; the handler reads
  `sample_text`/`sample_sender_id`, so every simulation ran against an empty
  message and reported "no match" for rules that would match.
* `rules settings reset` sent a literal `null` instead of the handler's `reset`
  list, and `--replace` switched HTTP verbs instead of setting `replace_all`.
* `rules enable` flipped state via the only mutation the API has (`toggle`), so
  enabling an enabled task disabled it. Lifecycle verbs now compare the state the
  server reports with the state the verb promised.
* `rules sources remove` dropped titles by value, deleting an unrelated source
  that shared a title; titles are positional and are now matched by index.
* `--set key=value` was passed as a list into a namespace-only helper, so
  `getattr(list, "set", [])` returned `[]` and the settings were accepted and
  ignored.
* `rules settings schema --group` read `settings` off the envelope instead of
  `schema.settings`, so a valid group looked like "no such settings".
* `ai check-key` treated HTTP 200 as success. The route answers 200 with
  `success: true` even when the provider rejects the key; the verdict is
  `healthy`.
* `prompts list` read the wrong envelope key (`templates`, not `prompts`).
* `--group` on a subparser overwrote the top-level `group` dest, sending
  dispatch down the wrong branch; subparser flags must not reuse a parent dest.

## Adding a command

1. Put the rules in `domain/` (a validator, a spec field, a value object).
2. Add the method to a use case in `application/use_cases/`, taking and returning
   domain types. Validate there; do not re-validate in the command.
3. Add the port method to the `Protocol` and implement it in
   `infrastructure/http/gateways.py`.
4. Register the subparser in `commands/<group>.py`; format only.
5. Add the group to `COMMAND_MODULES` in `cli.py` if it is new, and a regression
   test in `core/tests/` that fails without your fix.

`atf` is run as `python -m atfcli`, or as the packaged `atf.pyz`
(`atf release portable`).
