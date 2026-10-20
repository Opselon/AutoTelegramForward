/// Empty state primitive: icon, one sentence, one call to action.
///
/// A panel is never blank: this is the widget a list shows when the request
/// succeeded and genuinely returned nothing, as opposed to [ErrorState] (the
/// request failed) and [LoadingList] (a request is in flight).
library;

import 'package:flutter/material.dart';

import '../core/design_tokens.dart';
import 'app_button.dart';
import 'status_badge.dart';

class EmptyState extends StatelessWidget {
  const EmptyState({
    super.key,
    required this.title,
    this.subtitle,
    this.icon = Icons.inbox_outlined,
    this.actionLabel,
    this.onAction,
    this.actionIcon = Icons.add,
    this.tone,
    this.compact = false,
  });

  final String title;
  final String? subtitle;
  final IconData icon;
  final String? actionLabel;
  final VoidCallback? onAction;
  final IconData actionIcon;
  final StatusTone? tone;

  /// Tighter vertical rhythm, for an empty section inside a card.
  final bool compact;

  /// An empty state that is itself scrollable, so a [RefreshIndicator] around
  /// it still has something to drag. Without this the gesture is dead exactly
  /// when the user most wants to pull for fresh data ("it says empty — maybe
  /// try again").
  static Widget scrollable({
    Key? key,
    required String title,
    String? subtitle,
    IconData icon = Icons.inbox_outlined,
    String? actionLabel,
    VoidCallback? onAction,
    IconData actionIcon = Icons.add,
  }) {
    return LayoutBuilder(
      key: key,
      builder: (context, constraints) => SingleChildScrollView(
        physics: const AlwaysScrollableScrollPhysics(),
        child: ConstrainedBox(
          constraints: BoxConstraints(minHeight: constraints.maxHeight),
          child: Center(
            child: EmptyState(
              title: title,
              subtitle: subtitle,
              icon: icon,
              actionLabel: actionLabel,
              onAction: onAction,
              actionIcon: actionIcon,
            ),
          ),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final palette = AppColors.of(context);
    final theme = Theme.of(context);
    final accent = switch (tone) {
      StatusTone.success => palette.success,
      StatusTone.warning => palette.warning,
      StatusTone.danger => palette.danger,
      StatusTone.info => palette.info,
      StatusTone.neutral => palette.neutral,
      null => palette.primary,
    };

    return Semantics(
      container: true,
      label: subtitle == null ? title : '$title. $subtitle',
      child: Padding(
        padding: EdgeInsets.symmetric(
          horizontal: AppSpacing.xl,
          vertical: compact ? AppSpacing.lg : AppSpacing.xxl,
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              padding: const EdgeInsets.all(AppSpacing.md),
              decoration: BoxDecoration(
                color: accent.withValues(alpha: 0.12),
                shape: BoxShape.circle,
              ),
              child: Icon(icon, color: accent, size: compact ? 22 : 28),
            ),
            const SizedBox(height: AppSpacing.md),
            ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 420),
              child: Text(
                title,
                textAlign: TextAlign.center,
                style: theme.textTheme.titleSmall?.copyWith(
                  fontWeight: FontWeight.w600,
                ),
              ),
            ),
            if (subtitle != null && subtitle!.isNotEmpty) ...[
              const SizedBox(height: AppSpacing.sm),
              ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 420),
                child: Text(
                  subtitle!,
                  textAlign: TextAlign.center,
                  style: theme.textTheme.bodySmall,
                ),
              ),
            ],
            if (onAction != null && actionLabel != null) ...[
              const SizedBox(height: AppSpacing.lg),
              AppButton(
                label: actionLabel!,
                onPressed: onAction,
                icon: actionIcon,
                variant: AppButtonVariant.primary,
                size: AppButtonSize.sm,
              ),
            ],
          ],
        ),
      ),
    );
  }
}
