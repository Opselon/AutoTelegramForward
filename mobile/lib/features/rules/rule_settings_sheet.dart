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
        () =>
            _loadError =
                exc.error.message.isNotEmpty
                    ? exc.error.message
                    : t.get('rules_settings_load_failed'),
      );
    } catch (exc) {
      if (!mounted) return;
      setState(
        () =>
            _loadError =
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
          () =>
              _saveError =
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
        () =>
            _saveError =
                exc.error.message.isNotEmpty
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
                        onPressed:
                            _saving ? null : () => Navigator.of(context).pop(),
                        child: Text(
                          AppMessages.of(context).get('action_cancel'),
                        ),
                      ),
                      const SizedBox(width: 8),
                      FilledButton.icon(
                        icon:
                            _saving
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

    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
      children: [
        for (final group in ordered) ...[
          Padding(
            padding: const EdgeInsets.only(top: 6, bottom: 6),
            child: Text(
              _groupTitle(t, group),
              style: const TextStyle(
                fontSize: 12,
                fontWeight: FontWeight.w700,
                letterSpacing: 0.4,
              ),
            ),
          ),
          for (final spec in grouped[group]!) _field(spec),
          const SizedBox(height: 8),
        ],
      ],
    );
  }

  /// Group names come from the server registry (e.g. `content_cleanup`); they
  /// are identifiers, not catalog keys, so they are title-cased rather than
  /// translated — a new server-side group must not render as a raw key.
  static String _groupTitle(AppMessages t, String group) {
    if (group.isEmpty) return 'GENERAL';
    final words = group.split(RegExp(r'[_\s-]+'));
    return words
        .map((w) => w.isEmpty ? w : '${w[0].toUpperCase()}${w.substring(1)}')
        .join(' ')
        .toUpperCase();
  }

  Widget _field(RuleSettingSpec spec) {
    final value = _values[spec.key];
    final error = _errors[spec.key];

    if (spec.isBool) {
      return SwitchListTile(
        contentPadding: EdgeInsets.zero,
        dense: true,
        title: Text(spec.label, style: const TextStyle(fontSize: 13)),
        subtitle:
            spec.help.isEmpty
                ? null
                : Text(spec.help, style: const TextStyle(fontSize: 11)),
        value: value == true,
        onChanged: (v) => setState(() => _values[spec.key] = v),
      );
    }

    if (spec.choices.isNotEmpty) {
      final selected =
          value != null && spec.choices.contains('$value') ? '$value' : null;
      return Padding(
        padding: const EdgeInsets.only(bottom: 10),
        child: DropdownButtonFormField<String>(
          value: selected,
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
    if (spec.isDict) {
      return _textField(
        spec,
        value,
        error,
        lines: 3,
        hint: t.get('rules_settings_json_hint'),
        encode:
            (v) =>
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
          final parts =
              text
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
                _errors[spec.key] = AppMessages.of(
                  context,
                ).get('rules_settings_required');
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
              () =>
                  _errors[spec.key] = AppMessages.of(
                    context,
                  ).get('rules_settings_invalid', params: {'kind': spec.kind}),
            );
          } catch (_) {
            setState(
              () =>
                  _errors[spec.key] = AppMessages.of(
                    context,
                  ).get('rules_settings_invalid', params: {'kind': spec.kind}),
            );
          }
        },
      ),
    );
  }
}
