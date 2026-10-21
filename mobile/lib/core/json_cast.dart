/// Lenient reads for the numeric fields of an API payload.
///
/// The server sends several numeric values as strings, and that is not an
/// error: `topic_id` defaults to `""` in storage, Telegram ids travel as
/// strings so a 64-bit id survives a JavaScript client, and a version field can
/// arrive as `"1.2.2"`. A bare `json['x'] as num?` therefore throws
/// `type 'String' is not a subtype of type 'num?' in type cast` — a runtime
/// failure that blanks whatever screen was parsing it.
///
/// Every numeric read in the client goes through these functions instead, so a
/// server that stringifies a number degrades to the documented default rather
/// than taking the screen down. Each one accepts a number, a numeric string
/// (with surrounding whitespace), or anything else and yields the fallback.
library;

/// The numeric value of [value], or `null` when it is absent or not numeric.
///
/// `''`, `'   '`, `'1.2.2'`, `true`, a list and a map all read as `null`: none
/// of them is a number, and inventing one would be worse than saying nothing.
num? jsonNum(Object? value) {
  if (value is num) return value.isFinite ? value : null;
  if (value is String) {
    final trimmed = value.trim();
    if (trimmed.isEmpty) return null;
    // `num.tryParse` accepts 'NaN' and 'Infinity'; neither is a value this
    // client can render or send back, so both read as absent.
    final parsed = num.tryParse(trimmed);
    return (parsed != null && parsed.isFinite) ? parsed : null;
  }
  return null;
}

/// [jsonNum], truncated to an `int`, or [fallback].
int jsonInt(Object? value, [int fallback = 0]) =>
    jsonNum(value)?.toInt() ?? fallback;

/// [jsonNum] as an `int`, or `null` when the field is absent.
///
/// Use this when "the server did not say" must stay distinguishable from "the
/// server said zero" — an explicit zero is a real value for a rate or a count.
int? jsonIntOrNull(Object? value) => jsonNum(value)?.toInt();

/// [jsonNum] as a `double`, or [fallback].
double jsonDouble(Object? value, [double fallback = 0]) =>
    jsonNum(value)?.toDouble() ?? fallback;

/// The "forward every Nth message" interval, or 1 when the value is absent,
/// not a number, or outside the registry's documented range.
///
/// 1 is "every message" and the default for every rule created before the
/// setting existed, so anything unreadable degrades to legacy behaviour
/// rather than to silence.
int clampForwardEveryN(Object? value, [int fallback = 1]) {
  final n = jsonNum(value)?.toInt();
  if (n == null) return fallback;
  if (n < 1) return 1;
  if (n > 1000) return 1000;
  return n;
}

/// A `{name: count}` map whose values may be numbers or numeric strings.
Map<String, int> jsonIntMap(Object? value) {
  if (value is! Map) return const {};
  return {
    for (final entry in value.entries) '${entry.key}': jsonInt(entry.value),
  };
}

/// A list of strings that may arrive as strings, numbers, or anything else.
///
/// Task ids and names travel as a JSON array whose entries are practically
/// always strings, but a server-side change that emits a number (or a null
/// placeholder) must not take the whole list down with a cast. Non-string
/// entries are dropped rather than stringified: a `1` in a task-name list is
/// not a name, and inventing one would mislabel a row.
List<String> jsonStringList(Object? value) {
  if (value is! List) return const [];
  return value
      .whereType<String>()
      .where((e) => e.isNotEmpty)
      .toList(growable: false);
}
