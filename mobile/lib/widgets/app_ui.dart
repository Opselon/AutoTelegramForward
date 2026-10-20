/// Shared UI primitives so every screen renders the same visual language.
///
/// Before this file each screen hand-rolled its own card padding, badge shape
/// and empty/error state. That is how a dashboard ends up with three different
/// "green means online" pills. These widgets are the single source for:
///   * [SectionCard]  — titled container with an optional trailing action
///   * [StatTile]     — gradient metric tile used by the dashboard grids
///   * [StatusPill]   — online / offline / severity badge
///   * [GradientHeader] — brand header band shown above page content
///   * [InlineBanner] — error / warning / info strip with a retry affordance
///   * [EmptyStateView] — consistent "nothing here yet" panel
library;

import 'package:flutter/material.dart';

import '../core/theme.dart';
import '../l10n/app_messages.dart';
import 'app_card.dart';
import 'empty_state.dart';
import 'status_badge.dart';

/// Titled card. Now a thin adapter over [AppCard], so every screen that used
/// this widget inherits the design-system card (surface-2, radius `md`,
/// elevation 1, 16 dp padding) instead of keeping its own border colour.
class SectionCard extends StatelessWidget {
  const SectionCard({
    super.key,
    required this.title,
    this.trailing,
    this.icon,
    this.padding = AppSpacing.screen,
    required this.child,
  });

  final String title;
  final Widget? trailing;
  final IconData? icon;
  final EdgeInsets padding;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    return AppCard(
      title: title,
      icon: icon,
      trailing: trailing,
      padding: padding,
      child: child,
    );
  }
}

/// Gradient metric tile. `value` is emphasised; `caption` carries the
/// denominator or a unit ("3 / 12", "1.2 s").
class StatTile extends StatelessWidget {
  const StatTile({
    super.key,
    required this.label,
    required this.value,
    required this.icon,
    this.caption,
    this.gradient = AppGradients.brand,
    this.onTap,
  });

  final String label;
  final String value;
  final IconData icon;
  final String? caption;
  final Gradient gradient;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final radius = AppRadii.allLg;

    return DecoratedBox(
      decoration: BoxDecoration(
        gradient: gradient,
        borderRadius: radius,
        boxShadow: AppShadows.dark(theme.brightness),
      ),
      child: Material(
        color: Colors.transparent,
        child: InkWell(
          onTap: onTap,
          borderRadius: radius,
          child: Padding(
            padding: const EdgeInsets.all(AppSpacing.lg),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    Expanded(
                      child: Text(
                        label,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: Colors.white70,
                          fontSize: 11.5,
                          fontWeight: FontWeight.w600,
                          letterSpacing: 0.3,
                        ),
                      ),
                    ),
                    Container(
                      padding: const EdgeInsets.all(6),
                      decoration: BoxDecoration(
                        color: Colors.white.withValues(alpha: 0.18),
                        borderRadius: AppRadii.allSm,
                      ),
                      child: Icon(icon, size: 16, color: Colors.white),
                    ),
                  ],
                ),
                const SizedBox(height: AppSpacing.sm),
                Row(
                  crossAxisAlignment: CrossAxisAlignment.baseline,
                  textBaseline: TextBaseline.alphabetic,
                  children: [
                    Flexible(
                      child: Text(
                        value,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: Colors.white,
                          fontSize: 26,
                          fontWeight: FontWeight.w800,
                          letterSpacing: -0.8,
                        ),
                      ),
                    ),
                    if (caption != null) ...[
                      const SizedBox(width: AppSpacing.xs),
                      Flexible(
                        child: Text(
                          caption!,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            color: Colors.white70,
                            fontSize: 12,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ),
                    ],
                  ],
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// Compact status badge — the one place that decides what "online" looks like.
///
/// Kept as a named constructor pair for the screens that predate the design
/// system; the rendering (and therefore the colour of "success") is
/// [StatusBadge]'s.
class StatusPill extends StatelessWidget {
  const StatusPill({
    super.key,
    required this.label,
    required this.color,
    this.icon,
    this.dense = false,
  });

  /// Convenience constructor for the ubiquitous online/offline case.
  ///
  /// [label] is required: this widget has no `BuildContext` of its own, and the
  /// previous `'ONLINE'`/`'OFFLINE'` default rendered English in all nine other
  /// locales. Callers pass the catalog's own words.
  factory StatusPill.online(
    bool online, {
    required String label,
    bool dense = false,
  }) => StatusPill(
    label: label,
    color: AppTheme.statusColor(online),
    icon: online ? Icons.check_circle : Icons.cancel,
    dense: dense,
  );

  final String label;
  final Color color;
  final IconData? icon;
  final bool dense;

  /// Maps the legacy semantic colour onto a badge tone. Status colours are the
  /// only input, so this is a total function over the palette, not a guess.
  StatusTone get _tone {
    if (color == AppTheme.success) return StatusTone.success;
    if (color == AppTheme.warning) return StatusTone.warning;
    if (color == AppTheme.error) return StatusTone.danger;
    if (color == AppTheme.info) return StatusTone.info;
    return StatusTone.neutral;
  }

  @override
  Widget build(BuildContext context) =>
      StatusBadge(label: label, tone: _tone, icon: icon, dense: dense);
}

/// Readiness hero: the one-line answer to "is this server actually set up?".
///
/// Backed by `GET /api/system/setup`, which returns the verdict *and* the
/// evidence (per-service probes, capability list, version block). A green hero
/// with no services behind it would be a lie, so the caller passes the probed
/// service list through [serviceSummary].
class ReadinessHero extends StatelessWidget {
  const ReadinessHero({
    super.key,
    required this.ready,
    required this.title,
    required this.subtitle,
    required this.verdictLabel,
    required this.serviceSummary,
    this.onOpen,
    this.openLabel = '',
    this.footnote,
  });

  final bool ready;
  final String title;
  final String subtitle;
  final String verdictLabel;
  final String serviceSummary;
  final VoidCallback? onOpen;
  final String openLabel;
  final String? footnote;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final gradient = ready
        ? AppGradients.brand
        : const LinearGradient(
            begin: Alignment.topLeft,
            end: Alignment.bottomRight,
            colors: [Color(0xFFF59E0B), Color(0xFFEF4444)],
          );

    return Container(
      decoration: AppGlass.panel(
        brightness: theme.brightness,
        gradient: gradient,
        radius: AppRadii.lg,
      ),
      padding: const EdgeInsets.all(AppSpacing.lg),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  title,
                  style: theme.textTheme.titleMedium?.copyWith(
                    color: Colors.white,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
              Container(
                padding: const EdgeInsets.symmetric(
                  horizontal: AppSpacing.md,
                  vertical: 5,
                ),
                decoration: BoxDecoration(
                  color: Colors.white.withValues(alpha: 0.18),
                  borderRadius: AppRadii.allPill,
                  border: Border.all(
                    color: Colors.white.withValues(alpha: 0.35),
                  ),
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(
                      ready
                          ? Icons.verified_rounded
                          : Icons.report_problem_rounded,
                      size: 13,
                      color: Colors.white,
                    ),
                    const SizedBox(width: 4),
                    Text(
                      verdictLabel,
                      style: const TextStyle(
                        color: Colors.white,
                        fontSize: 11,
                        fontWeight: FontWeight.w700,
                        letterSpacing: 0.3,
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
          const SizedBox(height: AppSpacing.sm),
          Text(
            subtitle,
            style: theme.textTheme.bodySmall?.copyWith(
              color: Colors.white.withValues(alpha: 0.85),
            ),
          ),
          const SizedBox(height: AppSpacing.md),
          Row(
            children: [
              const Icon(Icons.hub_rounded, size: 14, color: Colors.white),
              const SizedBox(width: 6),
              Expanded(
                child: Text(
                  serviceSummary,
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 12,
                    fontWeight: FontWeight.w600,
                    fontFeatures: [FontFeature.tabularFigures()],
                  ),
                ),
              ),
            ],
          ),
          if (footnote != null && footnote!.isNotEmpty) ...[
            const SizedBox(height: AppSpacing.xs),
            Text(
              footnote!,
              style: TextStyle(
                color: Colors.white.withValues(alpha: 0.8),
                fontSize: 11,
              ),
            ),
          ],
          if (onOpen != null) ...[
            const SizedBox(height: AppSpacing.md),
            Align(
              alignment: AlignmentDirectional.centerStart,
              child: TextButton.icon(
                onPressed: onOpen,
                style: TextButton.styleFrom(
                  backgroundColor: Colors.white.withValues(alpha: 0.16),
                  foregroundColor: Colors.white,
                  padding: const EdgeInsets.symmetric(
                    horizontal: AppSpacing.lg,
                    vertical: AppSpacing.sm,
                  ),
                  shape: const RoundedRectangleBorder(
                    borderRadius: AppRadii.allPill,
                  ),
                ),
                icon: const Icon(Icons.arrow_forward_rounded, size: 16),
                label: Text(
                  openLabel,
                  style: const TextStyle(fontWeight: FontWeight.w700),
                ),
              ),
            ),
          ],
        ],
      ),
    );
  }
}

/// Capability chips: what this server actually implements, straight from
/// `/api/system/setup`. Unknown-because-older-server is shown as absence, not
/// as an error.
class FeatureChips extends StatelessWidget {
  const FeatureChips({
    super.key,
    required this.features,
    required this.emptyLabel,
    this.color,
  });

  final List<String> features;
  final String emptyLabel;
  final Color? color;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final tint = color ?? AppTheme.primary;
    if (features.isEmpty) {
      return Text(emptyLabel, style: theme.textTheme.bodySmall);
    }
    return Wrap(
      spacing: AppSpacing.sm,
      runSpacing: AppSpacing.sm,
      children: [
        for (final feature in features)
          Container(
            padding: const EdgeInsets.symmetric(
              horizontal: AppSpacing.md,
              vertical: 5,
            ),
            decoration: BoxDecoration(
              color: tint.withValues(alpha: 0.12),
              borderRadius: AppRadii.allPill,
              border: Border.all(color: tint.withValues(alpha: 0.35)),
            ),
            child: Text(
              feature,
              style: TextStyle(
                color: tint,
                fontSize: 11,
                fontWeight: FontWeight.w700,
                letterSpacing: 0.2,
              ),
            ),
          ),
      ],
    );
  }
}

/// One row of the configuration contract.
///
/// The API returns **booleans only** for configured keys - never the values -
/// so this widget has nothing to redact: it can only ever render a key name and
/// whether it is set.
class ContractRow extends StatelessWidget {
  const ContractRow({
    super.key,
    required this.keyName,
    required this.levelLabel,
    required this.reason,
    required this.stateLabel,
    required this.isSet,
    this.hint = '',
  });

  final String keyName;
  final String levelLabel;
  final String reason;
  final String stateLabel;
  final bool isSet;
  final String hint;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final color = isSet ? AppTheme.success : AppTheme.warning;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: AppSpacing.sm),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(
            isSet
                ? Icons.check_circle_outline_rounded
                : Icons.help_outline_rounded,
            size: 16,
            color: color,
          ),
          const SizedBox(width: AppSpacing.sm),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Expanded(
                      child: Text(
                        keyName,
                        style: theme.textTheme.bodyMedium?.copyWith(
                          fontFamily: 'monospace',
                          fontWeight: FontWeight.w600,
                          color: theme.colorScheme.onSurface,
                        ),
                      ),
                    ),
                    StatusPill(
                      label: '$stateLabel - $levelLabel',
                      color: color,
                      dense: true,
                    ),
                  ],
                ),
                if (reason.isNotEmpty)
                  Text(reason, style: theme.textTheme.bodySmall),
                if (hint.isNotEmpty && !isSet)
                  Padding(
                    padding: const EdgeInsets.only(top: 2),
                    child: Text(
                      '> $hint',
                      style: theme.textTheme.bodySmall?.copyWith(
                        color: theme.colorScheme.primary,
                      ),
                    ),
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

/// A probed microservice: state, endpoint detail and the measured latency.
class ServiceTile extends StatelessWidget {
  const ServiceTile({
    super.key,
    required this.name,
    required this.online,
    required this.detail,
    required this.latencyMs,
    required this.onlineLabel,
    required this.offlineLabel,
    required this.latencyLabel,
  });

  final String name;
  final bool online;
  final String detail;
  final int latencyMs;
  final String onlineLabel;
  final String offlineLabel;
  final String latencyLabel;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: AppSpacing.sm),
      child: Row(
        children: [
          StatusPill.online(online, label: online ? onlineLabel : offlineLabel),
          const SizedBox(width: AppSpacing.md),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(name.toUpperCase(), style: theme.textTheme.titleSmall),
                if (detail.isNotEmpty)
                  Text(
                    detail,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: theme.textTheme.bodySmall,
                  ),
              ],
            ),
          ),
          Text(
            latencyLabel,
            style: theme.textTheme.bodySmall?.copyWith(
              fontFeatures: const [FontFeature.tabularFigures()],
            ),
          ),
        ],
      ),
    );
  }
}

/// Brand header band used at the top of a page body.
class GradientHeader extends StatelessWidget {
  const GradientHeader({
    super.key,
    required this.title,
    this.subtitle,
    this.icon = Icons.bolt,
    this.trailing,
  });

  final String title;
  final String? subtitle;
  final IconData icon;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(AppSpacing.lg),
      decoration: BoxDecoration(
        gradient: AppGradients.brand,
        borderRadius: AppRadii.allLg,
        boxShadow: AppShadows.dark(Theme.of(context).brightness),
      ),
      child: Row(
        children: [
          Container(
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(
              color: Colors.white.withValues(alpha: 0.2),
              borderRadius: AppRadii.allMd,
            ),
            child: Icon(icon, color: Colors.white, size: 22),
          ),
          const SizedBox(width: AppSpacing.md),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  title,
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 19,
                    fontWeight: FontWeight.w800,
                    letterSpacing: -0.4,
                  ),
                ),
                if (subtitle != null) ...[
                  const SizedBox(height: 2),
                  Text(
                    subtitle!,
                    style: const TextStyle(color: Colors.white70, fontSize: 12),
                  ),
                ],
              ],
            ),
          ),
          ?trailing,
        ],
      ),
    );
  }
}

/// Error / warning / info strip with an optional retry action.
class InlineBanner extends StatelessWidget {
  const InlineBanner({
    super.key,
    required this.message,
    this.severity = 'error',
    this.onRetry,
    this.retryLabel,
  });

  final String message;
  final String severity;
  final VoidCallback? onRetry;
  final String? retryLabel;

  @override
  Widget build(BuildContext context) {
    final color = AppTheme.severityColor(severity);
    final icon = severity == 'error'
        ? Icons.error_outline
        : severity == 'warning'
        ? Icons.warning_amber_rounded
        : Icons.info_outline;

    return Container(
      padding: const EdgeInsets.all(AppSpacing.md),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.12),
        borderRadius: AppRadii.allMd,
        border: Border.all(color: color.withValues(alpha: 0.45)),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(icon, color: color, size: 20),
          const SizedBox(width: AppSpacing.sm),
          Expanded(
            child: Text(
              message,
              style: Theme.of(context).textTheme.bodySmall
                  ?.copyWith(color: Theme.of(context).colorScheme.onSurface),
            ),
          ),
          if (onRetry != null)
            TextButton(
              onPressed: onRetry,
              child: Text(
                retryLabel ?? AppMessages.of(context).get('action_retry'),
              ),
            ),
        ],
      ),
    );
  }
}

/// Consistent empty / "all good" panel. Adapter over [EmptyState].
class EmptyStateView extends StatelessWidget {
  const EmptyStateView({
    super.key,
    required this.message,
    this.icon = Icons.inbox_outlined,
    this.color,
  });

  final String message;
  final IconData icon;
  final Color? color;

  StatusTone get _tone {
    if (color == AppTheme.success) return StatusTone.success;
    if (color == AppTheme.warning) return StatusTone.warning;
    if (color == AppTheme.error) return StatusTone.danger;
    if (color == AppTheme.info) return StatusTone.info;
    if (color == AppTheme.primary) return StatusTone.info;
    return StatusTone.neutral;
  }

  @override
  Widget build(BuildContext context) =>
      EmptyState(title: message, icon: icon, tone: _tone, compact: true);
}
