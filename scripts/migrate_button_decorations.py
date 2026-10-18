"""One-shot migration: route hand-rolled button glyph prefixes through _btn().

Rewrites ``"<glyph> " + self._t("key", ...)`` into ``self._btn("key", ...)``
for every expression that ends up as a button label — inside ``_kbd(...)``
rows, inside ``InlineKeyboardButton(...)``, and inside a local variable that
is used as a button label. Glyph prefixes were applied by hand all over the
UI, which is how the same key ended up as "✖", "❌" and a doubled "❌ ❌".
"""
import ast
import re
import sys
from pathlib import Path

PATH = Path("core/infrastructure/telegram/pro_bot_ui.py")
GLYPH_ONLY = re.compile(
    r"^\s*[\U0001F000-\U0001FAFF\u2190-\u2BFF\u2600-\u27BF\u231A-\u23FF"
    r"\u2705\u274C\u2B50\u2714\u2716\u20E3\uFE0F\s]+$"
)


def call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def button_label_nodes(tree: ast.AST) -> list[ast.AST]:
    labels: list[ast.AST] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = call_name(node.func)
        if name in ("InlineKeyboardButton", "KeyboardButton") and node.args:
            labels.append(node.args[0])
        elif name in ("_kbd", "_cancel_kbd") and node.args:
            for element in ast.walk(node.args[0]):
                if isinstance(element, ast.Tuple) and len(element.elts) >= 2:
                    labels.append(element.elts[0])
    # Rows are usually built into a local list first (rows.append([(label,
    # data)]) ...) and only handed to _kbd() at the end, so those tuples are
    # labels too. They are recognised by their callback-data second element.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Tuple) or len(node.elts) not in (2, 3):
            continue
        data = node.elts[1]
        if isinstance(data, ast.Constant) and isinstance(data.value, str):
            if not re.search(r"\s", data.value):
                labels.append(node.elts[0])
        elif isinstance(data, ast.JoinedStr) or isinstance(
            data, (ast.Attribute, ast.Subscript)
        ):
            labels.append(node.elts[0])
    return labels


def main() -> int:
    source = PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    labels = button_label_nodes(tree)
    label_names = {
        node.id for node in labels if isinstance(node, ast.Name)
    }
    targets = list(labels)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in label_names:
                    targets.append(node.value)

    edits: list[tuple[int, int, int, int, str]] = []
    for node in targets:
        replacement_node = None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
                prefix, call = node.left.value, node.right
            elif isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
                prefix, call = node.right.value, node.left
            else:
                continue
            if not GLYPH_ONLY.match(prefix):
                continue
            replacement_node = (node, call)
        elif isinstance(node, ast.JoinedStr):
            # f"🔙 {self._t('ui_rule_back_rules')}" — a glyph glued to a
            # translated label by an f-string instead of a concatenation.
            if len(node.values) != 2:
                continue
            head, tail = node.values
            if not isinstance(head, ast.Constant) or not isinstance(head.value, str):
                continue
            if not GLYPH_ONLY.match(head.value):
                continue
            inner = getattr(tail, "value", None)
            if not isinstance(inner, ast.Call) or call_name(inner.func) != "_t":
                continue
            replacement_node = (node, inner)
        if replacement_node is None:
            continue
        label_node, call = replacement_node
        if not isinstance(call, ast.Call) or call_name(call.func) != "_t":
            continue
        if not isinstance(call.func, ast.Attribute):  # only self._t -> self._btn
            continue
        call_src = ast.get_source_segment(source, call)
        if call_src is None:
            continue
        replacement = call_src.replace(call.func.attr + "(", "_btn(", 1)
        edits.append(
            (label_node.lineno, label_node.col_offset, label_node.end_lineno, label_node.end_col_offset, replacement)
        )

    if not edits:
        print("no decorated button labels found")
        return 1

    # CPython reports col_offset/end_col_offset as UTF-8 *byte* offsets, and
    # button labels are full of multi-byte emoji — splice on encoded bytes.
    raw = source.encode("utf-8")
    offsets, total = [], 0
    for line in source.splitlines(keepends=True):
        offsets.append(total)
        total += len(line.encode("utf-8"))
    seen: set[tuple[int, int]] = set()
    for lineno, col, end_lineno, end_col, replacement in sorted(
        edits, key=lambda e: (e[0], e[1]), reverse=True
    ):
        key = (lineno, col)
        if key in seen:
            continue
        seen.add(key)
        start = offsets[lineno - 1] + col
        end = offsets[end_lineno - 1] + end_col
        raw = raw[:start] + replacement.encode("utf-8") + raw[end:]

    source = raw.decode("utf-8")
    ast.parse(source)
    PATH.write_text(source, encoding="utf-8")
    print(f"rewrote {len(seen)} decorated button labels through self._btn()")
    return 0


if __name__ == "__main__":
    sys.exit(main())
