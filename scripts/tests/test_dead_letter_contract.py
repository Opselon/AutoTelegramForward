"""Both queue backends must share one dead-letter contract.

The dead-letter entry is written by *repository code* that runs on whichever
backend is configured, while the *table* is created by each backend's own DDL.
So the column names are a contract between three files, and nothing enforced it:

* SQLite's table had `last_error` and `attempts`;
* PostgreSQL's had `error_message` and no `attempts` at all;
* there were two copies of the INSERT, and the copy in `mark_dead_letter` named
  `error_message` — a column SQLite does not have. It sat inside a bare
  ``except: pass``, so on the production backend that path recorded no entry and
  said nothing: a delivery nobody could see or retry.

A wrong column name here is invisible until someone needs the entry, which is
the worst time to find out. These checks fail on the shape of the code, so the
drift cannot come back quietly.

Also pinned: neither backend may silently fall back to the interface's no-op
defaults for the scoped read/replay/purge methods. A backend that implements the
method by inheriting `return []` looks wired up and quietly sees nothing.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_PERSISTENCE = REPO / "core" / "infrastructure" / "persistence"
#: The SQLite table is created in `database.py`; its accessors are in
#: `sqlite_repositories.py`. "This backend" spans both files, so code checks
#: read the pair while column checks read the DDL.
SQLITE_DDL = _PERSISTENCE / "database.py"
SQLITE_CODE = _PERSISTENCE / "sqlite_repositories.py"
POSTGRES = _PERSISTENCE / "postgres_repositories.py"
#: (DDL file, code file) per backend.
BACKENDS = ((SQLITE_DDL, SQLITE_CODE), (POSTGRES, POSTGRES))
#: The code files alone, for the checks that read one method's source.
CODE_FILES = (SQLITE_CODE, POSTGRES)
INTERFACE = REPO / "core" / "application" / "repositories.py"

#: Columns every backend's dead_letter_queue must define. `attempts` and
#: `last_error` are how a replayed delivery gets its budget back and how the
#: screen shows why it died, so both have to exist everywhere.
CONTRACT_COLUMNS = {
    "id",
    "job_id",
    "rule_id",
    "source_chat_id",
    "source_message_id",
    "target_chat_id",
    "payload_json",
    "attempts",
    "last_error",
    "error_category",
    "dead_lettered_at",
}

#: The one column a backend is allowed to have beyond the shared contract:
#: PostgreSQL's older name for `last_error`. It is migrated from, never used.
LEGACY_ONLY = {"error_message"}

#: The result codes both backends must speak and the interface must document.
RESULT_CODES = {"requeued", "not_found", "already_pending", "already_sent", "replay_failed"}

SCOPED_METHODS = ("list_dead_letters", "replay_dead_letter", "purge_dead_letters")

DDL_RE = re.compile(r"CREATE TABLE IF NOT EXISTS dead_letter_queue\s*\((.*?)\);", re.S)
COLUMN_RE = re.compile(r"^\s*([a-z_][a-z0-9_]*)\s+", re.M)
CONSTRAINT_WORDS = ("unique", "primary", "constraint", "foreign", "check")


def _columns(text: str) -> set[str]:
    match = DDL_RE.search(text)
    assert match, "dead_letter_queue DDL not found"
    body = "\n".join(
        segment
        for segment in re.split(r",\s*\n", match.group(1))
        if segment.strip() and not segment.strip().lower().startswith(CONSTRAINT_WORDS)
    )
    return {c.lower() for c in COLUMN_RE.findall(body)}


def _code_tokens(text: str, name: str) -> set[str]:
    """Every string constant and identifier a function's *code* uses.

    The docstring is excluded. Without that, narrative that mentions a legacy
    column name for the reader's benefit is indistinguishable from code that
    writes it — and the guard would then be satisfiable only by deleting the
    documentation, which is the wrong direction.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:  # pragma: no cover - the file is in CI, so it parses
        return set()
    tokens: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            doc = ast.get_docstring(node, clean=False)
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    if doc and sub.value == doc:
                        continue
                    tokens.add(sub.value)
                elif isinstance(sub, ast.Name):
                    tokens.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    tokens.add(sub.attr)
                elif isinstance(sub, ast.keyword) and sub.arg:
                    tokens.add(sub.arg)
    return tokens


def _backend_code(path: Path) -> str:
    """A backend's code file plus its DDL module, as one text."""
    text = path.read_text(encoding="utf-8")
    ddl = path.parent / "database.py"
    if ddl.exists() and ddl != path:
        text += "\n" + ddl.read_text(encoding="utf-8")
    return text


def _function_body(text: str, name: str) -> str:
    """The source of one method, from its ``def``/``async def`` to the next.

    Indentation is flattened first: inside a class the body is indented four
    spaces, but a repository may define one at module level too, and the
    boundary pattern has to match both.
    """
    # Dedent exactly one level: class-level methods then start at column 0
    # while a nested closure (`_unit`) keeps its own indentation, so it does not
    # look like the end of the method being read.
    flat = text.replace("\n    ", "\n")
    match = re.search(rf"^def {name}\(|^async def {name}\(", flat, re.M)
    assert match, f"{name} not found"
    rest = flat[match.end():]
    nxt = re.search(r"^def |^async def ", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


# --------------------------------------------------------------------- #
# One column contract
# --------------------------------------------------------------------- #
def test_both_backends_define_the_same_dead_letter_columns() -> None:
    sqlite_cols = _columns(SQLITE_DDL.read_text(encoding="utf-8"))
    pg_cols = _columns(POSTGRES.read_text(encoding="utf-8"))

    assert CONTRACT_COLUMNS <= sqlite_cols, f"sqlite is missing {CONTRACT_COLUMNS - sqlite_cols}"
    assert CONTRACT_COLUMNS <= pg_cols, f"postgres is missing {CONTRACT_COLUMNS - pg_cols}"

    # A subset check is not enough: a column that exists on one backend only is
    # the same drift in the other direction, and it fails the same way at
    # runtime — the repository writes `last_error`, so a backend that has a
    # differently-named extra column and not this one loses every entry.
    assert not sqlite_cols - pg_cols, (
        f"sqlite has columns postgres does not: {sqlite_cols - pg_cols}"
    )
    extra = pg_cols - sqlite_cols
    assert extra <= LEGACY_ONLY, (
        f"postgres has columns sqlite does not: {extra - LEGACY_ONLY}"
    )


def test_the_interface_documents_the_same_columns_it_promises() -> None:
    """`attempts`/`last_error` are the reason a replay can restore a budget."""
    text = INTERFACE.read_text(encoding="utf-8")
    body = _function_body(text, "replay_dead_letter")
    assert "attempt" in body.lower()
    assert "budget" in body.lower() or "attempts" in body


def test_no_dead_letter_code_writes_the_legacy_column() -> None:
    """`error_message` is PostgreSQL's old name. Writing it on SQLite throws,
    and the throw used to be swallowed."""
    for _, path in BACKENDS:
        text = path.read_text(encoding="utf-8")
        for name in ("mark_retry", "mark_dead_letter", "_dead_letter_row"):
            code = _code_tokens(text, name)
            for token in code:
                assert "error_message" not in token, (
                    f"{path.name}::{name} writes the legacy column; "
                    f"the contract column is `last_error`"
                )


def test_the_legacy_column_survives_only_as_migrated_data() -> None:
    """Nothing may read it: an entry is shown from `last_error` everywhere."""
    pg = POSTGRES.read_text(encoding="utf-8")
    legacy_lines = [ln for ln in pg.splitlines() if "error_message" in ln]
    assert legacy_lines, "the legacy column vanished; check the migration still backfills it"
    for line in legacy_lines:
        stripped = line.strip()
        # Allowed: the two column declarations, and the backfill that moves the
        # text into `last_error`. Anything else is a new reader/writer of the
        # legacy name, which is how the two backends drifted apart.
        declarative = stripped.startswith("error_message ")
        assert declarative or "last_error" in stripped or "Legacy" in stripped, (
            f"unexpected use of the legacy column: {stripped}"
        )


# --------------------------------------------------------------------- #
# One producer, and no silent no-ops
# --------------------------------------------------------------------- #
def test_each_backend_writes_the_entry_in_exactly_one_place() -> None:
    """Two copies of the INSERT is how the two copies disagreed."""
    for _, path in BACKENDS:
        text = _backend_code(path)
        inserts = re.findall(r"INSERT (?:OR REPLACE )?INTO dead_letter_queue", text)
        assert len(inserts) == 1, (
            f"{path.name} has {len(inserts)} dead-letter inserts; keep one producer"
        )
        assert "_dead_letter_row" in text


def test_both_backends_implement_the_scoped_methods() -> None:
    """Inheriting the interface's `return []` looks wired up and sees nothing."""
    for _, path in BACKENDS:
        text = path.read_text(encoding="utf-8")
        for name in SCOPED_METHODS:
            assert re.search(rf"\n    async def {name}\(", text), (
                f"{path.name} does not implement {name}"
            )


def test_a_replay_is_one_delivery_not_a_second_row() -> None:
    """`delivery_jobs` is UNIQUE(rule_id, source_chat_id, source_message_id,
    target_chat_id). A replay that inserted a new row instead of waking the
    existing one would be ignored by that constraint and report success."""
    for path in CODE_FILES:
        body = _function_body(path.read_text(encoding="utf-8"), "replay_dead_letter")
        assert "UPDATE delivery_jobs" in body, f"{path.name} does not wake the delivery up"
        # Woken, not merely touched: the job has to go back to PENDING or the
        # worker looks at it and ignores it, and the replay reports success.
        assert "status = 'PENDING'" in body or "status='PENDING'" in body, (
            f"{path.name} does not return the delivery to PENDING"
        )
        assert "UNIQUE" in body or "ON CONFLICT (rule_id" in body, (
            f"{path.name} does not explain/handle the one-delivery constraint"
        )


def test_the_result_codes_are_a_closed_set() -> None:
    """A caller switches on these; an undocumented code is an unhandled case."""
    text = INTERFACE.read_text(encoding="utf-8")
    documented = set(re.findall(r"``([a-z_]+)``\s+—", _function_body(text, "replay_dead_letter")))
    assert documented == RESULT_CODES, f"documented={documented} expected={RESULT_CODES}"
    for path in CODE_FILES:
        body = _function_body(path.read_text(encoding="utf-8"), "replay_dead_letter")
        returned = set(re.findall(r'return "([a-z_]+)"', body))
        assert returned <= RESULT_CODES, f"{path.name} returns {returned - RESULT_CODES}"
