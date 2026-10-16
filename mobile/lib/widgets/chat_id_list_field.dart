/// Multi-value channel input: comma-separated text plus verified chips.
///
/// One of these drives each side of a routing task. The text field is the only
/// edit surface (so a list can be pasted in one go), and the chips below it are
/// the verification result: one chip per parsed entry, marked accepted or
/// rejected with the reason, individually removable, and tappable to select
/// that entry back inside the field so a single wrong id can be fixed without
/// retyping the list.
///
/// The widget holds no state of its own — the caller owns the controller and
/// computes [entries] from it with `verifyRouteEntries` — so a chip can never
/// disagree with what will be saved.
library;

import 'package:flutter/material.dart';

import '../core/route_input.dart';
import '../core/theme.dart';
import '../l10n/app_messages.dart';

class ChatIdListField extends StatelessWidget {
  const ChatIdListField({
    super.key,
    required this.controller,
    required this.label,
    required this.helper,
    required this.emptyLabel,
    required this.entries,
    required this.onRemove,
    required this.onFix,
    this.fieldKey,
    this.focusNode,
    this.validator,
    this.icon = Icons.alt_route_rounded,
  });

  /// Raw comma-separated text. The single source of truth for this side.
  final TextEditingController controller;
  final String label;

  /// Format guidance shown under the field.
  final String helper;

  /// Shown instead of the chips while nothing has been typed.
  final String emptyLabel;
  final List<RouteEntry> entries;

  /// Drops one entry, leaving the rest of the list and its order alone.
  final ValueChanged<String> onRemove;

  /// Selects one entry inside the field for correction.
  final ValueChanged<String> onFix;

  /// Key for the inner text field, so tests can type into it directly.
  final Key? fieldKey;
  final FocusNode? focusNode;
  final String? Function(String?)? validator;
  final IconData icon;

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final theme = Theme.of(context);

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        TextFormField(
          key: fieldKey,
          controller: controller,
          focusNode: focusNode,
          validator: validator,
          minLines: 1,
          maxLines: 3,
          decoration: InputDecoration(
            labelText: label,
            helperText: helper,
            helperMaxLines: 3,
            prefixIcon: Icon(icon),
          ),
        ),
        const SizedBox(height: AppSpacing.sm),
        if (entries.isEmpty)
          Text(emptyLabel, style: theme.textTheme.bodySmall)
        else
          Wrap(
            spacing: AppSpacing.sm,
            runSpacing: AppSpacing.sm,
            children: [for (final entry in entries) _chip(t, entry)],
          ),
      ],
    );
  }

  /// One verified entry. Green means it will be part of the saved route, red
  /// means one of the route rules rejected it — the tooltip says which.
  Widget _chip(AppMessages t, RouteEntry entry) {
    final color = entry.isOk ? AppTheme.success : AppTheme.error;
    return Tooltip(
      message:
          entry.isOk
              ? t.get(entry.messageKey)
              : t.get(
                'rules_route_chip_tooltip',
                params: {'reason': t.get(entry.messageKey)},
              ),
      child: InputChip(
        key: ValueKey<String>('route_chip_${entry.value}'),
        label: Text(entry.value),
        avatar: Icon(
          entry.isOk ? Icons.check_circle_outline : Icons.error_outline,
          size: 16,
          color: color,
        ),
        backgroundColor: color.withValues(alpha: 0.12),
        side: BorderSide(color: color.withValues(alpha: 0.45)),
        deleteIcon: const Icon(Icons.close, size: 16),
        deleteButtonTooltipMessage: t.get('rules_route_remove'),
        onPressed: () => onFix(entry.value),
        onDeleted: () => onRemove(entry.value),
      ),
    );
  }
}
