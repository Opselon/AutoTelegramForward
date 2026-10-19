// Regression gate: no hardcoded, user-facing English copy in the app.
//
// AGENTS.md requires every label the user reads to resolve from the catalog
// (`AppMessages`), which is kept 1:1 across the ten shipped locales. A literal
// like `labelText: 'Task Name'` renders English in all nine other languages,
// and no analyzer rule catches it - so this test walks the real sources.
//
// The scan is deliberately conservative: it only inspects positions that are
// *rendered* - a `Text`/`RichText`/`TextSpan` argument, or the value of a
// display parameter (`labelText`, `hintText`, `tooltip`, `title`, `label`, ...).
// A literal that reaches the screen through one of those and is not a catalog
// lookup fails the test. Technical tokens (URLs, format examples, brand and
// protocol names) are exempt through [technicalTokens], each with the reason it
// is not copy.
//
// Two views of every file are built first: comments blanked out, and string
// bodies blanked out as well (interpolation included). Offsets stay in place,
// so line numbers are real, a parenthesis inside a string cannot unbalance the
// structural scan, and prose in a `///` comment cannot register as copy.
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

/// Calls whose argument list is rendered as text.
const Set<String> _displayCalls = <String>{
  'Text',
  'SelectableText',
  'RichText',
  'TextSpan',
};

/// Named parameters whose value is rendered as text.
const Set<String> _displayParams = <String>{
  'labelText',
  'hintText',
  'helperText',
  'errorText',
  'counterText',
  'tooltip',
  'label',
  'title',
  'subtitle',
  'semanticLabel',
  'emptyTitle',
  'emptySubtitle',
};

/// Values that are legitimately identical in every locale.
///
/// One entry per technical token, with the reason it is not copy. Anything
/// absent from this list must come from `t.get(...)`.
const Map<String, String> technicalTokens = <String, String>{
  // The product name is a brand; it is never translated.
  'AutoTelegramForward': 'brand name',
  // The transport the route card labels itself with. A brand name, and the
  // server's own chat-type vocabulary (`telegram`), not a sentence.
  'Telegram': 'platform brand name',
  // A URL example shown as a hint, not a sentence.
  'https://atf.example.com': 'URL example',
  // Bot-token shape example (`123456789:AA…`), documentation only.
  '123456789:AA…': 'credential format example',
  // The generated task-name shape: an identifier echoed back, not a label.
  'af_route_lab_b': 'generated task-name example',
  // Service names exactly as the logger stores them. They are the filter
  // values sent back to the API and are never translated, in the bot either.
  'core': 'logger service identifier',
  'api': 'logger service identifier',
  'logger': 'logger service identifier',
  'web': 'logger service identifier',
};

/// A `'...'`/`"..."` literal found in a source file.
class _Literal {
  _Literal(this.start, this.text);

  final int start;
  final String text;
}

/// Two offset-aligned views of one file: [source] (comments blanked) and
/// [code] (comments and string bodies blanked).
class _Views {
  _Views(this.source, this.code);

  final String source;
  final String code;
}

void main() {
  group('hardcoded UI copy', () {
    test('every rendered label resolves through the catalog', () {
      final offenders = <String>[];
      final files = _sourceFiles();
      expect(
        files.length,
        greaterThan(10),
        reason: 'the scan must find mobile/lib sources, not an empty tree',
      );

      for (final file in files) {
        final raw = file.readAsStringSync();
        final views = _views(raw);
        for (final literal in _literals(views.source)) {
          if (!_isRendered(views.code, literal.start)) continue;
          if (!_looksLikeCopy(literal.text)) continue;
          if (technicalTokens.containsKey(literal.text)) continue;
          final line = raw.substring(0, literal.start).split('\n').length;
          offenders.add('${_relative(file)}:$line: ${literal.text}');
        }
      }

      expect(
        offenders,
        isEmpty,
        reason:
            'These labels are hardcoded English. Move each one into '
            'mobile/lib/l10n (all ten locales) and read it with t.get(...):\n'
            '${offenders.join('\n')}',
      );
    });

    test('the technical-token allowlist is still necessary', () {
      // Keeps the allowlist from rotting into a blanket: every exempt value
      // must still exist in the tree, so a removed example is removed here
      // too instead of silently shielding a future leak.
      final sources = _sourceFiles()
          .map((f) => f.readAsStringSync())
          .join('\n');
      for (final token in technicalTokens.keys) {
        expect(
          sources,
          contains("'$token'"),
          reason: 'stale allowlist entry: $token',
        );
      }
    });
  });
}

/// Every `.dart` file under `lib/`, minus the catalogs themselves.
///
/// The catalogs under `lib/l10n/` hold values by definition, so scanning them
/// would only report translations as offenders.
List<File> _sourceFiles() {
  final root = Directory('lib').existsSync()
      ? Directory('lib')
      : Directory('mobile/lib');
  expect(
    root.existsSync(),
    isTrue,
    reason: 'run from the mobile package root (lib/ not found)',
  );
  return root
      .listSync(recursive: true)
      .whereType<File>()
      .where((f) => f.path.endsWith('.dart'))
      .where((f) => !f.path.replaceAll(r'\', '/').contains('/l10n/'))
      .toList()
    ..sort((a, b) => a.path.compareTo(b.path));
}

String _relative(File file) => file.path.replaceAll(r'\', '/');

_Views _views(String src) {
  final source = src.split('');
  final code = src.split('');
  final n = src.length;
  var i = 0;

  void blank(int index) {
    if (src[index] != '\n') code[index] = ' ';
  }

  while (i < n) {
    final c = src[i];
    if (c == '/' && i + 1 < n && src[i + 1] == '/') {
      while (i < n && src[i] != '\n') {
        source[i] = ' ';
        code[i] = ' ';
        i++;
      }
      continue;
    }
    if (c == '/' && i + 1 < n && src[i + 1] == '*') {
      while (i < n && !(src[i] == '*' && i + 1 < n && src[i + 1] == '/')) {
        source[i] = ' ';
        blank(i);
        i++;
      }
      if (i < n) {
        source[i] = ' ';
        code[i] = ' ';
        if (i + 1 < n) {
          source[i + 1] = ' ';
          code[i + 1] = ' ';
        }
        i += 2;
      }
      continue;
    }
    if (c != "'" && c != '"') {
      i++;
      continue;
    }
    if (i + 2 < n && src[i + 1] == c && src[i + 2] == c) {
      // Block string: keep the body (it is often a doc example), drop it from
      // the structural view.
      final end = src.indexOf(c * 3, i + 3);
      final stop = end == -1 ? n : end + 3;
      for (var k = i; k < stop; k++) {
        blank(k);
      }
      i = stop;
      continue;
    }
    blank(i);
    i++;
    while (i < n) {
      final ch = src[i];
      if (ch == r'\') {
        blank(i);
        if (i + 1 < n) blank(i + 1);
        i += 2;
        continue;
      }
      if (ch == r'$') {
        blank(i);
        if (i + 1 < n && src[i + 1] == '{') {
          var depth = 0;
          var k = i + 1;
          while (k < n) {
            final ck = src[k];
            if (ck == '{') depth++;
            if (ck == '}') {
              depth--;
              if (depth == 0) {
                blank(k);
                k++;
                break;
              }
            }
            blank(k);
            k++;
          }
          i = k;
          continue;
        }
        i++;
        while (i < n && _isIdentChar(src[i])) {
          blank(i);
          i++;
        }
        continue;
      }
      if (ch == c) {
        blank(i);
        i++;
        break;
      }
      blank(i);
      i++;
    }
  }
  return _Views(source.join(), code.join());
}

/// Literals in [source] (the comment-stripped view).
List<_Literal> _literals(String source) {
  final out = <_Literal>[];
  final n = source.length;
  var i = 0;
  while (i < n) {
    final c = source[i];
    if (c != "'" && c != '"') {
      i++;
      continue;
    }
    if (i + 2 < n && source[i + 1] == c && source[i + 2] == c) {
      final end = source.indexOf(c * 3, i + 3);
      i = end == -1 ? n : end + 3;
      continue;
    }
    final start = i;
    i++;
    var closed = false;
    while (i < n) {
      final ch = source[i];
      if (ch == r'\') {
        i += 2;
        continue;
      }
      if (ch == r'$' && i + 1 < n && source[i + 1] == '{') {
        var depth = 0;
        i++;
        while (i < n) {
          if (source[i] == '{') depth++;
          if (source[i] == '}') {
            depth--;
            if (depth == 0) {
              i++;
              break;
            }
          }
          i++;
        }
        continue;
      }
      if (ch == c) {
        out.add(_Literal(start, source.substring(start + 1, i)));
        closed = true;
        i++;
        break;
      }
      i++;
    }
    if (!closed) break;
  }
  return out;
}

/// True when the literal at [pos] is rendered.
///
/// Walks outwards: the innermost enclosing `(` decides. A grouping paren (no
/// call name in front of it) is not a decision - the walk continues outwards,
/// which is what catches `label ?? (online ? 'ONLINE' : 'OFFLINE')`.
bool _isRendered(String code, int pos) {
  var depth = 0;
  var i = pos - 1;
  while (i >= 0) {
    final c = code[i];
    if (c == ')' || c == ']' || c == '}') {
      depth++;
    } else if (c == '(' || c == '[' || c == '{') {
      if (depth > 0) {
        depth--;
      } else if (c == '(') {
        final name = _identifierBefore(code, i);
        if (name == 'get') return false; // a catalog lookup
        if (name == 'Key' || name == 'ValueKey' || name == 'UniqueKey') {
          return false; // a widget identity, never rendered
        }
        if (_displayCalls.contains(name)) return true;
        // Named parameter inside this argument list, e.g. `hintText:`.
        final param = _lastParamBefore(code, i + 1, pos);
        if (param != null) {
          if (param == 'key') return false; // widget identity, never rendered
          return _displayParams.contains(param);
        }
      } else if (depth == 0) {
        // `json['title']`: an index/map key, not an argument.
        return false;
      }
    }
    i--;
  }
  return false;
}

String _identifierBefore(String code, int open) {
  var j = open - 1;
  while (j >= 0 && _isSpace(code[j])) {
    j--;
  }
  var end = j;
  if (j >= 0 && code[j] == '>') {
    // Skip a generic argument list: `ValueKey<String>(` is still `ValueKey`.
    var depth = 0;
    while (j >= 0) {
      if (code[j] == '>') {
        depth++;
      } else if (code[j] == '<') {
        depth--;
        if (depth == 0) {
          j--;
          break;
        }
      }
      j--;
    }
    while (j >= 0 && _isSpace(code[j])) {
      j--;
    }
    end = j;
  }
  while (j >= 0 && _isIdentChar(code[j])) {
    j--;
  }
  return code.substring(j + 1, end + 1);
}

/// The last `name:` at bracket depth 0 in `code[from..to]`, if any.
String? _lastParamBefore(String code, int from, int to) {
  String? found;
  var depth = 0;
  var i = from;
  while (i < to) {
    final c = code[i];
    if (c == '(' || c == '[' || c == '{') {
      depth++;
      i++;
      continue;
    }
    if (c == ')' || c == ']' || c == '}') {
      depth--;
      i++;
      continue;
    }
    if (c == ':' && depth == 0 && (i + 1 >= to || code[i + 1] != ':')) {
      var j = i - 1;
      while (j >= from && _isSpace(code[j])) {
        j--;
      }
      final end = j;
      while (j >= from && _isIdentChar(code[j])) {
        j--;
      }
      final name = code.substring(j + 1, end + 1);
      if (name.isNotEmpty && !RegExp(r'^[0-9]').hasMatch(name)) {
        found = name;
      }
      i++;
      continue;
    }
    i++;
  }
  return found;
}

bool _isSpace(String c) => c == ' ' || c == '\t' || c == '\n' || c == '\r';

bool _isIdentChar(String c) => RegExp(r'[A-Za-z0-9_$]').hasMatch(c);

/// True when the literal carries words a translator would have to translate.
///
/// Interpolations are removed first: `'#${rule.shortId}'` and `'• $note'` are
/// value composition, not copy. Two consecutive letters are required, so
/// separators (`' • '`, `' → '`) and bare numbers never register.
bool _looksLikeCopy(String text) {
  final plain = text
      .replaceAll(RegExp(r'\$\{[^}]*\}'), '')
      .replaceAll(RegExp(r'\$[A-Za-z_][A-Za-z0-9_]*'), '');
  return RegExp(r'[A-Za-z]{2,}').hasMatch(plain);
}
