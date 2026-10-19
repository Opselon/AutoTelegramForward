// Shared list empty/error/loading state view.
//
// The screens must distinguish three states that were previously conflated
// into a single "empty" view:

import 'package:flutter/material.dart';

import '../l10n/app_messages.dart';

///   * [isLoading] — a request is in flight; show a spinner, not "no data".
///   * [error] is non-empty — the request FAILED (dead endpoint, auth, 5xx);
///     the user must see what went wrong, otherwise an unreachable server
///     looks identical to "you have no rules".
///   * otherwise — the request genuinely returned zero rows; that is the only
///     case where the "nothing here yet" empty state is honest.
class ListStateView extends StatelessWidget {
  const ListStateView({
    super.key,
    required this.isEmpty,
    required this.isLoading,
    this.error,
    required this.emptyTitle,
    this.emptySubtitle,
    required this.onRetry,
  });

  final bool isEmpty;
  final bool isLoading;
  final String? error;
  final String emptyTitle;
  final String? emptySubtitle;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final t = AppMessages.of(context);

    if (isLoading) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const CircularProgressIndicator(),
              const SizedBox(height: 12),
              Text(t.get('state_loading'), style: theme.textTheme.bodySmall),
            ],
          ),
        ),
      );
    }

    if (error != null && error!.isNotEmpty) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.cloud_off, size: 40, color: theme.colorScheme.error),
              const SizedBox(height: 12),
              Text(
                t.get('state_load_failed'),
                style: theme.textTheme.titleMedium?.copyWith(
                  fontWeight: FontWeight.w600,
                ),
              ),
              const SizedBox(height: 6),
              ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 320),
                child: Text(
                  error!,
                  textAlign: TextAlign.center,
                  style: theme.textTheme.bodySmall,
                ),
              ),
              const SizedBox(height: 16),
              OutlinedButton.icon(
                onPressed: onRetry,
                icon: const Icon(Icons.refresh, size: 18),
                label: Text(t.get('action_retry')),
              ),
            ],
          ),
        ),
      );
    }

    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              Icons.inbox_outlined,
              size: 40,
              color: theme.colorScheme.outline,
            ),
            const SizedBox(height: 12),
            Text(
              emptyTitle,
              textAlign: TextAlign.center,
              style: theme.textTheme.titleMedium?.copyWith(
                fontWeight: FontWeight.w600,
              ),
            ),
            if (emptySubtitle != null) ...[
              const SizedBox(height: 6),
              ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 320),
                child: Text(
                  emptySubtitle!,
                  textAlign: TextAlign.center,
                  style: theme.textTheme.bodySmall,
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}
