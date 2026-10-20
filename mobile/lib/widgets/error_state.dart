/// Error state primitive: the reason, in words the user can act on, plus Retry.
///
/// The retry label and the headline come from the ten-locale catalog, so a
/// failing request never renders an English button inside a Persian screen.
library;

import 'package:flutter/material.dart';

import '../core/design_tokens.dart';
import '../l10n/app_messages.dart';
import 'app_button.dart';

class ErrorState extends StatelessWidget {
  const ErrorState({
    super.key,
    required this.message,
    required this.onRetry,
    this.title,
    this.retryLabel,
  });

  /// The failure detail. Empty means "we know it failed, not why".
  final String message;
  final VoidCallback onRetry;

  /// Defaults to the catalog's "could not load" headline.
  final String? title;
  final String? retryLabel;

  @override
  Widget build(BuildContext context) {
    final palette = AppColors.of(context);
    final theme = Theme.of(context);
    final t = AppMessages.of(context);
    final headline = title ?? t.get('state_load_failed');

    return Center(
      child: Padding(
        padding: const EdgeInsets.all(AppSpacing.xl),
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 420),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Container(
                padding: const EdgeInsets.all(AppSpacing.md),
                decoration: BoxDecoration(
                  color: palette.danger.withValues(alpha: 0.12),
                  shape: BoxShape.circle,
                ),
                child: Icon(
                  Icons.cloud_off_rounded,
                  size: 28,
                  color: palette.danger,
                ),
              ),
              const SizedBox(height: AppSpacing.md),
              // liveRegion: the failure is announced when it appears instead of
              // requiring the user to hunt for what changed.
              Semantics(
                liveRegion: true,
                child: Text(
                  headline,
                  textAlign: TextAlign.center,
                  style: theme.textTheme.titleSmall?.copyWith(
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
              if (message.isNotEmpty) ...[
                const SizedBox(height: AppSpacing.sm),
                Text(
                  message,
                  textAlign: TextAlign.center,
                  maxLines: 4,
                  overflow: TextOverflow.ellipsis,
                  style: theme.textTheme.bodySmall,
                ),
              ],
              const SizedBox(height: AppSpacing.lg),
              AppButton(
                label: retryLabel ?? t.get('action_retry'),
                onPressed: onRetry,
                icon: Icons.refresh,
                variant: AppButtonVariant.secondary,
                size: AppButtonSize.sm,
              ),
            ],
          ),
        ),
      ),
    );
  }
}
