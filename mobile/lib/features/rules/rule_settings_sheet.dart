/// Full-control editor for one forwarding task.
///
/// The server owns the registry: it ships every setting's name, type, group,
/// default and bounds, and it validates writes. This sheet renders whatever the
/// registry describes, so a setting added on the server appears here without a
/// client release, and a value the server would reject is caught before the
/// round-trip.
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../core/models.dart';
import '../../l10n/app_messages.dart';

/// Opens the settings editor for [ruleId]. Safe to call from anywhere with a
/// Provider scope above it.
Future<void> showRuleSettingsSheet(
  BuildContext context, {
  required String ruleId,
  String ruleLabel = '',
}) {
  return showModalBottomSheet<void>(
    context: context,
    showDragHandle: true,
    isScrollControlled: true,
    builder: (_) => RuleSettingsSheet(ruleId: ruleId, ruleLabel: ruleLabel),
  );
}

class RuleSettingsSheet extends StatefulWidget {
  const RuleSettingsSheet({
    super.key,
    required this.ruleId,
    this.ruleLabel = '',
  });

  final String ruleId;
  final String ruleLabel;

  @override
  State<RuleSettingsSheet> createState() => _RuleSettingsSheetState();
}

class _RuleSettingsSheetState extends State<RuleSettingsSheet> {
  RuleSettingsSchema? _schema;

  /// Effective values, keyed by setting name. Edits mutate this map so the
  /// sheet can diff it against [_original] to send only real changes.
  final Map<String, dynamic> _values = {};
  final Map<String, dynamic> _original = {};
  final Map<String, String> _errors = {};

  bool _loading = true;
  bool _saving = false;
  String? _loadError;
  String? _saveError;

  /// Live filter over the whole schema. With ~120 settings, scrolling to find
  /// "remove_links" is the slow path; typing three letters is the fast one.
  String _query = '';

  /// The keys offered in the quick-cleanup card, in the order a user fixes a
  /// channel: strip the decoration, then the links, then the noise.
  ///
  /// These are ordinary registry keys — pinning them changes only where they
  /// are rendered, never what they do or how they are validated.
  static const List<String> _quickKeys = <String>[
    'remove_emojis',
    'remove_links',
    'block_links',
    'clean_usernames',
    'clean_hashtags',
    'clean_phones',
    'clean_empty_lines',
    'strip_words',
  ];

  @override
  void initState() {
    super.initState();
    // Deferred: reading Provider in initState is illegal, and the client is
    // only available through the inherited scope.
    WidgetsBinding.instance.addPostFrameCallback((_) => _load());
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _loadError = null;
    });
    final client = context.read<AppState>().buildApiClient();
    try {
      final schema = await client.ruleSettingsSchema();
      final current = await client.getRuleSettings(widget.ruleId);
      if (!mounted) return;
      setState(() {
        _schema = schema;
        _values
          ..clear()
          ..addAll(current);
        _original
          ..clear()
          ..addAll(current);
        _errors.clear();
      });
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      final t = AppMessages.of(context);
      setState(
        () => _loadError = exc.error.message.isNotEmpty
            ? exc.error.message
            : t.get('rules_settings_load_failed'),
      );
    } catch (exc) {
      if (!mounted) return;
      setState(
        () => _loadError =
            '${AppMessages.of(context).get('rules_settings_load_failed')}: $exc',
      );
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Map<String, dynamic> get _changed {
    final out = <String, dynamic>{};
    _values.forEach((key, value) {
      if (!_sameValue(_original[key], value)) out[key] = value;
    });
    return out;
  }

  /// Compares loosely: a setting that came back as `1` and is now `1.0` is not
  /// a change, and neither is int/double drift on a numeric field.
  static bool _sameValue(Object? a, Object? b) {
    if (a == b) return true;
    if (a is num && b is num) return a == b;
    if (a is List && b is List) {
      return a.length == b.length &&
          List.generate(
            a.length,
            (i) => i,
          ).every((i) => _sameValue(a[i], b[i]));
    }
    if (a is Map && b is Map) {
      return a.length == b.length &&
          a.keys.every((k) => b.containsKey(k) && _sameValue(a[k], b[k]));
    }
    return false;
  }

  /// Rejects bad input locally so the user sees the problem next to the field
  /// instead of a generic 400. The server re-validates regardless.
  bool _validate() {
    final t = AppMessages.of(context);
    final errors = <String, String>{};
    final schema = _schema;
    if (schema == null) return false;
    for (final spec in schema.settings) {
      final value = _values[spec.key];
      if (value == null) {
        if (!spec.nullable && spec.defaultValue != null) {
          errors[spec.key] = t.get('rules_settings_required');
        }
        continue;
      }
      if (spec.isNumber && value is num) {
        if (spec.minimum != null && value < spec.minimum!) {
          errors[spec.key] = t.get(
            'rules_settings_min',
            params: {'min': '${spec.minimum}'},
          );
        } else if (spec.maximum != null && value > spec.maximum!) {
          errors[spec.key] = t.get(
            'rules_settings_max',
            params: {'max': '${spec.maximum}'},
          );
        }
      }
      if (spec.choices.isNotEmpty && !spec.choices.contains('$value')) {
        errors[spec.key] = t.get(
          'rules_settings_invalid',
          params: {'kind': spec.choices.join(', ')},
        );
      }
    }
    setState(() {
      _errors
        ..clear()
        ..addAll(errors);
    });
    return errors.isEmpty;
  }

  Future<void> _save() async {
    final t = AppMessages.of(context);
    if (!_validate()) return;
    final changed = _changed;
    if (changed.isEmpty) {
      Navigator.of(context).pop();
      return;
    }
    setState(() {
      _saving = true;
      _saveError = null;
    });
    final client = context.read<AppState>().buildApiClient();
    final messenger = ScaffoldMessenger.of(context);
    final navigator = Navigator.of(context);
    try {
      final resp = await client.updateRuleSettings(widget.ruleId, changed);
      if (!mounted) return;
      if (resp['success'] == false) {
        setState(
          () => _saveError =
              '${resp['message'] ?? t.get('rules_settings_save_failed')}'
              '${resp['error_code'] != null ? ' (${resp['error_code']})' : ''}',
        );
        return;
      }
      navigator.pop();
      messenger.showSnackBar(
        SnackBar(
          content: Text(
            t.get('rules_settings_applied', params: {'n': '${changed.length}'}),
          ),
        ),
      );
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      setState(
        () => _saveError = exc.error.message.isNotEmpty
            ? exc.error.message
            : t.get('rules_settings_save_failed'),
      );
    } catch (exc) {
      if (!mounted) return;
      setState(
        () => _saveError = '${t.get('rules_settings_save_failed')}: $exc',
      );
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final height = MediaQuery.of(context).size.height;
    return SafeArea(
      child: ConstrainedBox(
        constraints: BoxConstraints(maxHeight: height * 0.9),
        child: Padding(
          padding: EdgeInsets.only(
            bottom: MediaQuery.of(context).viewInsets.bottom,
          ),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // ------------------------------------------------------- header
              Padding(
                padding: const EdgeInsets.fromLTRB(20, 0, 12, 8),
                child: Row(
                  children: [
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            t.get('rules_full_control'),
                            style: const TextStyle(
                              fontSize: 16,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                          if (widget.ruleLabel.isNotEmpty)
                            Text(
                              widget.ruleLabel,
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: const TextStyle(
                                fontSize: 12,
                                color: Colors.grey,
                              ),
                            ),
                        ],
                      ),
                    ),
                    if (_schema != null)
                      Padding(
                        padding: const EdgeInsets.only(right: 4),
                        child: Text(
                          t.get(
                            'rules_settings_count',
                            params: {'n': '${_schema!.settings.length}'},
                          ),
                          style: const TextStyle(
                            fontSize: 11,
                            color: Colors.grey,
                          ),
                        ),
                      ),
                    IconButton(
                      icon: const Icon(Icons.refresh),
                      tooltip: t.get('rules_settings_reload'),
                      onPressed: _loading ? null : _load,
                    ),
                  ],
                ),
              ),
              const Divider(height: 1),

              // -------------------------------------------------------- search
              if (_schema != null && _loadError == null)
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
                  child: TextField(
                    key: const ValueKey<String>('rules_settings_search'),
                    style: const TextStyle(fontSize: 13),
                    decoration: InputDecoration(
                      isDense: true,
                      prefixIcon: const Icon(Icons.search, size: 18),
                      suffixIcon: _query.isEmpty
                          ? null
                          : IconButton(
                              icon: const Icon(Icons.close, size: 16),
                              onPressed: () => setState(() => _query = ''),
                            ),
                      hintText: t.get('rules_settings_search_hint'),
                      border: const OutlineInputBorder(),
                    ),
                    onChanged: (v) => setState(() => _query = v.trim()),
                  ),
                ),

              // --------------------------------------------------------- body
              Expanded(child: _buildBody()),

              // ------------------------------------------------------- action
              if (_schema != null && _loadError == null) ...[
                const Divider(height: 1),
                if (_saveError != null)
                  Padding(
                    padding: const EdgeInsets.fromLTRB(20, 10, 20, 0),
                    child: Text(
                      _saveError!,
                      style: const TextStyle(fontSize: 12, color: Colors.red),
                    ),
                  ),
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 10, 16, 12),
                  child: Row(
                    children: [
                      Expanded(
                        child: Text(
                          _changed.isEmpty
                              ? t.get('rules_settings_no_changes')
                              : t.get(
                                  'rules_settings_change_count',
                                  params: {'n': '${_changed.length}'},
                                ),
                          style: const TextStyle(
                            fontSize: 12,
                            color: Colors.grey,
                          ),
                        ),
                      ),
                      TextButton(
                        onPressed: _saving
                            ? null
                            : () => Navigator.of(context).pop(),
                        child: Text(
                          AppMessages.of(context).get('action_cancel'),
                        ),
                      ),
                      const SizedBox(width: 8),
                      FilledButton.icon(
                        icon: _saving
                            ? const SizedBox(
                                width: 14,
                                height: 14,
                                child: CircularProgressIndicator(
                                  strokeWidth: 2,
                                ),
                              )
                            : const Icon(Icons.save_outlined, size: 18),
                        label: Text(t.get('rules_settings_apply')),
                        onPressed: _saving ? null : _save,
                      ),
                    ],
                  ),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildBody() {
    final t = AppMessages.of(context);
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_loadError != null) {
      return Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            const Icon(Icons.cloud_off, size: 32, color: Colors.grey),
            const SizedBox(height: 10),
            Text(
              _loadError!,
              textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 13),
            ),
            const SizedBox(height: 12),
            OutlinedButton.icon(
              icon: const Icon(Icons.refresh, size: 16),
              label: Text(t.get('action_retry')),
              onPressed: _load,
            ),
          ],
        ),
      );
    }
    final schema = _schema;
    if (schema == null || schema.settings.isEmpty) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Text(
            t.get('rules_settings_unavailable'),
            textAlign: TextAlign.center,
            style: const TextStyle(fontSize: 13),
          ),
        ),
      );
    }

    final grouped = schema.byGroup;
    // Server-chosen group order first, then anything it did not name.
    final ordered = <String>[
      ...schema.groups.where(grouped.containsKey),
      ...grouped.keys.where((g) => !schema.groups.contains(g)),
    ];

    // A query turns the grouped catalogue into a flat result list: a match in
    // the middle of "cleanup" is useless if the user has to find the group too.
    if (_query.isNotEmpty) {
      final needle = _query.toLowerCase();
      final hits = schema.settings
          .where(
            (spec) =>
                spec.key.toLowerCase().contains(needle) ||
                spec.label.toLowerCase().contains(needle) ||
                spec.help.toLowerCase().contains(needle),
          )
          .toList();
      if (hits.isEmpty) {
        return Center(
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: Text(
              t.get('rules_settings_no_match', params: {'q': _query}),
              textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 13),
            ),
          ),
        );
      }
      return ListView(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
        children: [
          for (final spec in hits) ...[
            Text(
              spec.key,
              style: const TextStyle(
                fontSize: 10,
                letterSpacing: 0.4,
                color: Colors.grey,
              ),
            ),
            _field(spec),
          ],
        ],
      );
    }

    // The quick card only shows keys this server actually offers, so a build
    // against an older core degrades to the plain grouped list instead of
    // rendering switches for settings that do not exist.
    final pinned = <RuleSettingSpec>[
      for (final key in _quickKeys)
        ...schema.settings.where((spec) => spec.key == key),
    ];

    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
      children: [
        if (pinned.isNotEmpty) ...[
          Row(
            children: [
              const Icon(Icons.auto_fix_high_outlined, size: 16),
              const SizedBox(width: 6),
              Expanded(
                child: Text(
                  t.get('rules_settings_quick_cleanup'),
                  style: const TextStyle(
                    fontSize: 12,
                    fontWeight: FontWeight.w700,
                    letterSpacing: 0.4,
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 2),
          Text(
            t.get('rules_settings_quick_cleanup_hint'),
            style: const TextStyle(fontSize: 11, color: Colors.grey),
          ),
          for (final spec in pinned) _field(spec),
          const Divider(height: 18),
        ],
        for (final group in ordered) ...[
          Padding(
            padding: const EdgeInsets.only(top: 6, bottom: 6),
            child: Text(
              ruleSettingsGroupTitle(t, group),
              style: const TextStyle(
                fontSize: 12,
                fontWeight: FontWeight.w700,
                letterSpacing: 0.4,
              ),
            ),
          ),
          // Pinned keys are rendered once, above: repeating them inside their
          // group would give two switches for one value and let them disagree.
          for (final spec in grouped[group]!)
            if (!_quickKeys.contains(spec.key)) _field(spec),
          const SizedBox(height: 8),
        ],
      ],
    );
  }

  Widget _field(RuleSettingSpec spec) {
    final value = _values[spec.key];
    final error = _errors[spec.key];

    if (spec.isBool) {
      return SwitchListTile(
        contentPadding: EdgeInsets.zero,
        dense: true,
        title: Text(spec.label, style: const TextStyle(fontSize: 13)),
        subtitle: spec.help.isEmpty
            ? null
            : Text(spec.help, style: const TextStyle(fontSize: 11)),
        value: value == true,
        onChanged: (v) => setState(() => _values[spec.key] = v),
      );
    }

    if (spec.choices.isNotEmpty) {
      final selected = value != null && spec.choices.contains('$value')
          ? '$value'
          : null;
      return Padding(
        padding: const EdgeInsets.only(bottom: 10),
        child: DropdownButtonFormField<String>(
          initialValue: selected,
          isDense: true,
          decoration: InputDecoration(
            isDense: true,
            labelText: spec.label,
            helperText: spec.help.isEmpty ? null : spec.help,
            errorText: error,
            border: const OutlineInputBorder(),
          ),
          items: [
            for (final choice in spec.choices)
              DropdownMenuItem(value: choice, child: Text(choice)),
          ],
          onChanged: (v) => setState(() => _values[spec.key] = v),
        ),
      );
    }

    final t = AppMessages.of(context);

    // `replacements` is the one dict that is genuinely a flat find/replace list,
    // so it gets a row editor. Every other dict holds structured data — nested
    // cleaner config, per-source overrides — where rows would lie about the
    // shape, so it keeps the JSON field.
    if (spec.key == 'replacements') {
      final raw = value is Map ? value : const <String, dynamic>{};
      return _PairEditor(
        key: const ValueKey<String>('replacements_editor'),
        label: spec.label,
        help: spec.help,
        error: error,
        initial: <String, String>{
          for (final entry in raw.entries) '${entry.key}': '${entry.value}',
        },
        onChanged: (map) => setState(() {
          _values[spec.key] = map;
          _errors.remove(spec.key);
        }),
      );
    }

    if (spec.isDict) {
      return _textField(
        spec,
        value,
        error,
        lines: 3,
        hint: t.get('rules_settings_json_hint'),
        encode: (v) =>
            v is Map ? const JsonEncoder.withIndent('  ').convert(v) : '$v',
        decode: (text, spec) {
          final decoded = jsonDecode(text);
          return decoded is Map ? decoded : <String, dynamic>{};
        },
      );
    }

    if (spec.isList) {
      return _textField(
        spec,
        value,
        error,
        hint: t.get('rules_settings_list_hint'),
        encode: (v) => v is List ? v.join(', ') : '$v',
        decode: (text, spec) {
          final parts = text
              .split(',')
              .map((e) => e.trim())
              .where((e) => e.isNotEmpty)
              .toList();
          // A list of numbers must stay numeric: sending "30" where the server
          // expects 30 is a type error, not a coercion.
          if (spec.itemKind == 'int' || spec.itemKind == 'float') {
            return parts
                .map((p) => num.tryParse(p) ?? p)
                .toList(growable: false);
          }
          return parts;
        },
      );
    }

    if (spec.isNumber) {
      return _textField(
        spec,
        value,
        error,
        keyboard: const TextInputType.numberWithOptions(
          decimal: true,
          signed: true,
        ),
        hint: t.get(
          spec.kind == 'int'
              ? 'rules_settings_int_hint'
              : 'rules_settings_number_hint',
        ),
        encode: (v) => '$v',
        decode: (text, spec) {
          final parsed = num.tryParse(text.trim());
          if (parsed == null) throw const FormatException('not a number');
          return spec.kind == 'int' ? parsed.toInt() : parsed.toDouble();
        },
      );
    }

    return _textField(spec, value, error, encode: (v) => '$v');
  }

  /// A text-backed field for every non-boolean kind. `encode`/`decode` convert
  /// between the wire value and the editable string, so the same widget serves
  /// numbers, lists and JSON objects.
  Widget _textField(
    RuleSettingSpec spec,
    Object? value,
    String? error, {
    int lines = 1,
    String? hint,
    TextInputType? keyboard,
    required String Function(Object? value) encode,
    Object? Function(String text, RuleSettingSpec spec)? decode,
  }) {
    final controller = TextEditingController(
      text: value == null ? '' : encode(value),
    );
    // Put the cursor at the end so editing a prefilled value does not select
    // the whole thing on focus.
    controller.selection = TextSelection.collapsed(
      offset: controller.text.length,
    );

    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: TextField(
        controller: controller,
        keyboardType: keyboard,
        maxLines: lines,
        style: const TextStyle(fontSize: 13),
        decoration: InputDecoration(
          isDense: true,
          labelText: spec.label,
          hintText: hint,
          helperText: spec.help.isEmpty ? null : spec.help,
          errorText: error,
          border: const OutlineInputBorder(),
        ),
        onChanged: (text) {
          final trimmed = text.trim();
          if (trimmed.isEmpty) {
            setState(() {
              if (spec.nullable) {
                _values[spec.key] = null;
                _errors.remove(spec.key);
              } else {
                _errors[spec.key] = AppMessages.of(context)
                    .get('rules_settings_required');
              }
            });
            return;
          }
          try {
            final parsed = decode == null ? trimmed : decode(trimmed, spec);
            setState(() {
              _values[spec.key] = parsed;
              _errors.remove(spec.key);
            });
          } on FormatException {
            setState(
              () => _errors[spec.key] = AppMessages.of(context)
                  .get('rules_settings_invalid', params: {'kind': spec.kind}),
            );
          } catch (_) {
            setState(
              () => _errors[spec.key] = AppMessages.of(context)
                  .get('rules_settings_invalid', params: {'kind': spec.kind}),
            );
          }
        },
      ),
    );
  }
}

/// One find/replace pair, with its own controllers so a rebuild elsewhere in
/// the sheet cannot move the caret while the user is typing.
class _PairRow {
  _PairRow(String find, String replace)
    : find = TextEditingController(text: find),
      replace = TextEditingController(text: replace);

  final TextEditingController find;
  final TextEditingController replace;

  void dispose() {
    find.dispose();
    replace.dispose();
  }
}

/// Row editor for the flat `replacements` map.
///
/// The registry stores it as a dict, so the generic renderer would offer raw
/// JSON — unusable with a phone keyboard. The rows rebuild that dict on every
/// keystroke, which keeps `_values` exactly equal to what a save would send.
class _PairEditor extends StatefulWidget {
  const _PairEditor({
    super.key,
    required this.initial,
    required this.label,
    required this.help,
    required this.error,
    required this.onChanged,
  });

  final Map<String, String> initial;
  final String label;
  final String help;
  final String? error;
  final ValueChanged<Map<String, String>> onChanged;

  @override
  State<_PairEditor> createState() => _PairEditorState();
}

class _PairEditorState extends State<_PairEditor> {
  late final List<_PairRow> _rows;
  bool _duplicate = false;

  @override
  void initState() {
    super.initState();
    // `widget.initial` is only a starting point: the rows are the truth after
    // the first keystroke, so a later rebuild must not resurrect old values.
    _rows = [
      for (final entry in widget.initial.entries)
        _PairRow(entry.key, entry.value),
    ];
  }

  @override
  void dispose() {
    for (final row in _rows) {
      row.dispose();
    }
    super.dispose();
  }

  /// Writes the rows back into a map and hands it to the sheet.
  ///
  /// A blank "find" is a row the user has not filled in yet, so it is skipped
  /// rather than saved as an empty-string rule; a repeated "find" is flagged,
  /// because one of the two rules would silently win.
  void _emit() {
    final map = <String, String>{};
    var duplicate = false;
    for (final row in _rows) {
      final find = row.find.text.trim();
      if (find.isEmpty) continue;
      if (map.containsKey(find)) duplicate = true;
      map[find] = row.replace.text;
    }
    setState(() => _duplicate = duplicate);
    widget.onChanged(map);
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final problem = _duplicate
        ? t.get('rules_settings_pair_duplicate')
        : widget.error;
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Icon(Icons.find_replace_outlined, size: 16),
              const SizedBox(width: 6),
              Expanded(
                child: Text(widget.label, style: const TextStyle(fontSize: 13)),
              ),
            ],
          ),
          if (widget.help.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 2, bottom: 4),
              child: Text(
                widget.help,
                style: const TextStyle(fontSize: 11, color: Colors.grey),
              ),
            ),
          for (var i = 0; i < _rows.length; i++)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Row(
                children: [
                  Expanded(
                    child: TextField(
                      key: ValueKey<String>('replace_find_$i'),
                      controller: _rows[i].find,
                      style: const TextStyle(fontSize: 13),
                      decoration: InputDecoration(
                        isDense: true,
                        hintText: t.get('rules_settings_pair_find'),
                        border: const OutlineInputBorder(),
                      ),
                      onChanged: (_) => _emit(),
                    ),
                  ),
                  const Padding(
                    padding: EdgeInsets.symmetric(horizontal: 6),
                    child: Icon(Icons.arrow_forward, size: 16),
                  ),
                  Expanded(
                    child: TextField(
                      key: ValueKey<String>('replace_with_$i'),
                      controller: _rows[i].replace,
                      style: const TextStyle(fontSize: 13),
                      decoration: InputDecoration(
                        isDense: true,
                        hintText: t.get('rules_settings_pair_with'),
                        border: const OutlineInputBorder(),
                      ),
                      onChanged: (_) => _emit(),
                    ),
                  ),
                  IconButton(
                    key: ValueKey<String>('replace_remove_$i'),
                    icon: const Icon(Icons.close, size: 16),
                    tooltip: t.get('rules_settings_pair_remove'),
                    onPressed: () {
                      setState(() {
                        _rows.removeAt(i).dispose();
                      });
                      _emit();
                    },
                  ),
                ],
              ),
            ),
          Align(
            alignment: AlignmentDirectional.centerStart,
            child: TextButton.icon(
              key: const ValueKey<String>('replace_add'),
              onPressed: () {
                setState(() => _rows.add(_PairRow('', '')));
              },
              icon: const Icon(Icons.add, size: 16),
              label: Text(t.get('rules_settings_pair_add')),
            ),
          ),
          if (problem != null)
            Text(
              problem,
              style: const TextStyle(fontSize: 11, color: Colors.red),
            ),
        ],
      ),
    );
  }
}

/// Header for one settings group in the sheet.
///
/// Group names come from the server registry (`general`, `cleanup`, `media`,
/// `branding`, …) and are identifiers, not catalog keys. A group that ships an
/// authored translation uses it; every other group — including one a newer
/// server adds — is title-cased, so a new server-side group can never render as
/// a raw identifier.
String ruleSettingsGroupTitle(AppMessages t, String group) {
  final key = _groupLabelKeys[group];
  if (key != null) return t.get(key);
  if (group.isEmpty) return 'GENERAL';
  final words = group.split(RegExp(r'[_\s-]+'));
  return words
      .map((w) => w.isEmpty ? w : '${w[0].toUpperCase()}${w.substring(1)}')
      .join(' ')
      .toUpperCase();
}

/// Registry group -> catalog key for the groups with an authored label. The
/// keys exist in every one of the ten catalogs (the parity audit enforces it).
const Map<String, String> _groupLabelKeys = {
  'branding': 'rules_settings_group_branding',
  // Who the copy is sent as (account or an attached bot) — a server group the
  // dashboard and the sheet both render, so it needs an authored label.
  'route': 'rules_settings_group_route',
};
