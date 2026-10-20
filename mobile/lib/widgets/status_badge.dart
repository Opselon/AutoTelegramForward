/// Badge primitive: a status pill in one of five tones, one line, truncating
/// with a tooltip so a long localized label cannot break the layout.
library;

import 'package:flutter/material.dart';

import '../core/design_tokens.dart';

enum StatusTone { success, warning, danger, info, neutral }

class StatusBadge extends StatelessWidget {
  const StatusBadge({
    super.key,
    required this.label,
    required this.tone,
    this.icon,
    this.dense = false,
  });

  /// Convenience for the ubiquitous online/offline case.
  factory StatusBadge.online(
    bool online, {
    required String label,
    bool dense = false,
  }) => StatusBadge(
    label: label,
    tone: online ? StatusTone.success : StatusTone.danger,
    icon: online ? Icons.check_circle : Icons.cancel,
    dense: dense,
  );

  /// Maps a severity string from the API onto a tone, so every surface paints
  /// "error" red and "warning" amber from one rule.
  factory StatusBadge.severity(
    String severity, {
    required String label,
    IconData? icon,
    bool dense = false,
  }) => StatusBadge(
    label: label,
    tone: switch (severity) {
      'error' => StatusTone.danger,
      'warning' => StatusTone.warning,
      'success' => StatusTone.success,
      _ => StatusTone.info,
    },
    icon: icon,
    dense: dense,
  );

  final String label;
  final StatusTone tone;
  final IconData? icon;
  final bool dense;

  @override
  Widget build(BuildContext context) {
    final palette = AppColors.of(context);
    final color = switch (tone) {
      StatusTone.success => palette.success,
      StatusTone.warning => palette.warning,
      StatusTone.danger => palette.danger,
      StatusTone.info => palette.info,
      StatusTone.neutral => palette.neutral,
    };

    final text = Text(
      label,
      maxLines: 1,
      overflow: TextOverflow.ellipsis,
      style: TextStyle(
        color: color,
        fontSize: dense ? 10 : AppType.xs,
        fontWeight: FontWeight.w700,
        letterSpacing: 0.3,
      ),
    );

    return Semantics(
      label: label,
      container: true,
      child: Tooltip(
        message: label,
        child: Container(
          padding: EdgeInsets.symmetric(
            horizontal: dense ? 7 : 9,
            vertical: dense ? 2 : 4,
          ),
          decoration: BoxDecoration(
            color: color.withValues(alpha: 0.14),
            borderRadius: AppRadius.allFull,
            border: Border.all(color: color.withValues(alpha: 0.4)),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (icon != null) ...[
                Icon(icon, size: dense ? 11 : 13, color: color),
                const SizedBox(width: AppSpacing.xs),
              ],
              Flexible(child: text),
            ],
          ),
        ),
      ),
    );
  }
}
