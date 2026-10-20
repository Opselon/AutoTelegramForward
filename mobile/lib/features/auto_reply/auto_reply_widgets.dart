/// Small building blocks shared by the auto-reply screens.
///
/// The feature is a form-heavy one, and four hand-rolled copies of "a labelled
/// dropdown" is how the app grows four different control heights. These
/// wrappers keep every control on the design system's primitives
/// ([AppField], choice chips, [SwitchListTile]) and keep the call sites short.
///
/// Everything here is presentation only: labels arrive already localised, so
/// there is no `BuildContext`-based string lookup that could bypass the catalog.
library;

import 'package:flutter/material.dart';

import '../../core/auto_reply_models.dart';
import '../../core/design_tokens.dart';
import '../../l10n/app_messages.dart';
import '../../widgets/app_field.dart';

/// One choice in a dropdown: the value sent to the API and its label.
@immutable
class ArOption {
  const ArOption(this.value, this.label);

  final String value;
  final String label;
}

/// A labelled dropdown backed by [DropdownButtonFormField].
class ArDropdown extends StatelessWidget {
  const ArDropdown({
    super.key,
    required this.label,
    required this.value,
    required this.options,
    required this.onChanged,
    this.helper,
  });

  final String label;
  final String value;
  final List<ArOption> options;
  final ValueChanged<String> onChanged;
  final String? helper;

  @override
  Widget build(BuildContext context) {
    final palette = AppColors.of(context);
    // A value the server knows but the list does not (an archived prompt, a
    // language added server-side) must still render: dropping it would silently
    // reset the field on the next save.
    final known = options.any((o) => o.value == value);
    final items = [
      ...options,
      if (!known) ArOption(value, value.isEmpty ? '—' : value),
    ];
    return Padding(
      padding: const EdgeInsets.only(bottom: AppSpacing.md),
      child: DropdownButtonFormField<String>(
        initialValue: value,
        isExpanded: true,
        decoration: InputDecoration(
          labelText: label,
          helperText: helper,
          helperMaxLines: 2,
          isDense: true,
          border: OutlineInputBorder(borderRadius: AppRadius.allMd),
          enabledBorder: OutlineInputBorder(
            borderRadius: AppRadius.allMd,
            borderSide: BorderSide(color: palette.border),
          ),
        ),
        dropdownColor: palette.surface2,
        items: [
          for (final option in items)
            DropdownMenuItem<String>(
              value: option.value,
              child: Text(option.label, overflow: TextOverflow.ellipsis),
            ),
        ],
        onChanged: (next) {
          if (next != null) onChanged(next);
        },
      ),
    );
  }
}

/// A row of mutually exclusive chips (tone, length, language, target mode).
class ArChips extends StatelessWidget {
  const ArChips({
    super.key,
    required this.options,
    required this.selected,
    required this.onSelected,
    this.keyPrefix = 'ar_chip',
  });

  final List<ArOption> options;
  final String selected;
  final ValueChanged<String> onSelected;

  /// Unique per group so a test (or a screen reader) can address one chip.
  final String keyPrefix;

  @override
  Widget build(BuildContext context) {
    return Wrap(
      spacing: AppSpacing.sm,
      runSpacing: AppSpacing.sm,
      children: [
        for (final option in options)
          ChoiceChip(
            key: ValueKey<String>('${keyPrefix}_${option.value}'),
            label: Text(option.label),
            selected: option.value == selected,
            onSelected: (_) => onSelected(option.value),
          ),
      ],
    );
  }
}

/// A switch row with a title and an explanatory line.
class ArSwitch extends StatelessWidget {
  const ArSwitch({
    super.key,
    required this.title,
    required this.value,
    required this.onChanged,
    this.subtitle,
    this.enabled = true,
  });

  final String title;
  final String? subtitle;
  final bool value;
  final ValueChanged<bool> onChanged;
  final bool enabled;

  @override
  Widget build(BuildContext context) {
    final palette = AppColors.of(context);
    // The nearest Material must sit directly above the tile: the card around
    // this row paints its own background with a DecoratedBox, which would hide
    // the tile's ink and trip the framework's "invisible splash" assertion.
    return Material(
      type: MaterialType.transparency,
      child: SwitchListTile.adaptive(
        contentPadding: EdgeInsets.zero,
        title: Text(title),
        subtitle: subtitle == null
            ? null
            : Text(subtitle!, style: TextStyle(color: palette.textMuted)),
        value: value,
        onChanged: enabled ? onChanged : null,
        dense: true,
      ),
    );
  }
}

/// A numeric text field that reports a parsed value and never a crash.
///
/// Out-of-range input is passed through as typed: the server validates and its
/// refusal is shown verbatim, which is better than the client silently clamping
/// a value the operator meant to change.
class ArNumberField extends StatelessWidget {
  const ArNumberField({
    super.key,
    required this.label,
    required this.value,
    required this.onChanged,
    this.helper,
    this.decimal = false,
  });

  final String label;
  final num value;
  final ValueChanged<num> onChanged;
  final String? helper;
  final bool decimal;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: AppSpacing.md),
      child: AppField(
        label: label,
        initialValue: decimal ? '${value.toDouble()}' : '${value.toInt()}',
        helper: helper,
        keyboardType: TextInputType.numberWithOptions(decimal: decimal),
        onChanged: (raw) {
          final parsed = decimal
              ? double.tryParse(raw.trim())
              : int.tryParse(raw.trim());
          if (parsed != null) onChanged(parsed);
        },
      ),
    );
  }
}

/// A labelled free-form text field (one or more lines).
class ArTextField extends StatelessWidget {
  const ArTextField({
    super.key,
    required this.label,
    required this.value,
    required this.onChanged,
    this.helper,
    this.hint,
    this.maxLines = 1,
    this.minLines,
  });

  final String label;
  final String value;
  final ValueChanged<String> onChanged;
  final String? helper;
  final String? hint;
  final int maxLines;
  final int? minLines;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: AppSpacing.md),
      child: AppField(
        label: label,
        initialValue: value,
        helper: helper,
        hint: hint,
        maxLines: maxLines,
        minLines: minLines,
        textCapitalization: TextCapitalization.sentences,
        onChanged: onChanged,
      ),
    );
  }
}

/// Tone options for the settings screen and the editors.
List<ArOption> arToneOptions(AppMessages t) => [
  for (final tone in const [
    'friendly',
    'casual',
    'professional',
    'formal',
    'playful',
    'empathetic',
    'direct',
  ])
    ArOption(tone, t.get(autoReplyToneKey(tone))),
];

/// Response-length options.
List<ArOption> arLengthOptions(AppMessages t) => [
  for (final length in const ['short', 'medium', 'long'])
    ArOption(length, t.get(autoReplyLengthKey(length))),
];

/// Reply-language options: "match the sender" first, then the languages the
/// catalogs name. A stored code outside the list still renders via [ArDropdown].
List<ArOption> arLanguageOptions(AppMessages t) => [
  for (final code in const [
    '',
    'en',
    'fa',
    'ar',
    'ru',
    'es',
    'fr',
    'it',
    'pt',
    'id',
    'uz',
    'zh',
    'tr',
    'de',
  ])
    ArOption(code, t.get(autoReplyLanguageKey(code))),
];

/// The seven weekday labels, Monday first (the API's 0..6).
List<ArOption> arDayOptions(AppMessages t) => [
  for (var i = 0; i < 7; i++) ArOption('$i', t.get('ar_day_$i')),
];
