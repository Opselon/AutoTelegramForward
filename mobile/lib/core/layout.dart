/// Adaptive layout contract (see `docs/ui-ux-system.md` §2).
///
/// Window size classes, exactly as the spec publishes them:
///   * **compact**  `< 600 dp` — bottom [NavigationBar], single column;
///   * **medium**   `600–839 dp` — labelled [NavigationRail], single column;
///   * **expanded** `>= 840 dp`  — rail plus list/detail two-pane.
///
/// Three primitives implement it so no screen re-derives a breakpoint:
///   * [AppWindow.of] — the resolved size class and text scale;
///   * [AppPageBody] — the centred, width-constrained scroll body (720 dp max,
///     so a tablet does not stretch one column across the whole width);
///   * [MasterDetailPane] — master list beside a detail pane on expanded, and
///     the master alone (which navigates) everywhere narrower.
library;

import 'package:flutter/material.dart';

import 'design_tokens.dart';

/// The three size classes from the spec.
enum AppWindowSize { compact, medium, expanded }

/// Breakpoint values. One place; a screen must never spell `840` itself.
class AppBreakpoints {
  const AppBreakpoints._();

  /// `< 600 dp` is compact: bottom navigation bar.
  static const double medium = 600;

  /// `>= 840 dp` is expanded: rail plus two-pane.
  static const double expanded = 840;

  /// Readable single-column width on a tablet.
  static const double contentMax = 720;

  /// Width of the master list in a two-pane layout.
  static const double masterPane = 360;

  /// Detail pane is never squeezed narrower than this.
  static const double detailMin = 420;

  /// Minimum touch target, per the accessibility contract.
  static const double touchTarget = 48;
}

/// Resolved window facts for one build.
@immutable
class AppWindow {
  const AppWindow({required this.size, required this.textScale});

  final Size size;
  final double textScale;

  factory AppWindow.of(BuildContext context) {
    final media = MediaQuery.maybeOf(context);
    if (media == null) {
      return const AppWindow(size: Size(400, 800), textScale: 1);
    }
    return AppWindow(
      size: media.size,
      textScale: media.textScaler.scale(AppType.md) / AppType.md,
    );
  }

  /// Shortest side, so a landscape phone is laid out by its height class, not
  /// by its 800 dp width (which would wrongly claim a tablet).
  double get shortestSide => size.shortestSide;

  AppWindowSize get sizeClass {
    final width = size.width;
    if (width >= AppBreakpoints.expanded) return AppWindowSize.expanded;
    if (width >= AppBreakpoints.medium) return AppWindowSize.medium;
    return AppWindowSize.compact;
  }

  bool get isCompact => sizeClass == AppWindowSize.compact;
  bool get isMedium => sizeClass == AppWindowSize.medium;
  bool get isExpanded => sizeClass == AppWindowSize.expanded;

  /// Two-pane needs the wide class *and* enough height: a 900×320 landscape
  /// phone cannot show a list and a detail pane without shrinking both to
  /// uselessness, so it keeps the single column.
  bool get supportsTwoPane =>
      isExpanded && size.height >= 480 && textScale <= 1.3;

  /// Horizontal page padding for the class: tighter on a phone.
  EdgeInsets get pagePadding => EdgeInsets.symmetric(
    horizontal: isCompact ? AppSpacing.lg : AppSpacing.xl,
    vertical: AppSpacing.lg,
  );
}

/// Centred, width-constrained page body.
///
/// On a phone it is a no-op beyond the padding; on a tablet or in landscape it
/// stops a single column of forms from spanning 1280 dp of empty width, which
/// is the "stretched phone UI" the spec forbids.
class AppPageBody extends StatelessWidget {
  const AppPageBody({
    super.key,
    required this.child,
    this.maxWidth = AppBreakpoints.contentMax,
    this.padding,
    this.scrollable = false,
    this.safeArea = true,
  });

  final Widget child;
  final double maxWidth;
  final EdgeInsets? padding;

  /// Wrap in a scroll view. Callers that already own a [ListView] leave this
  /// false; forms and detail panes set it so a short screen cannot overflow.
  final bool scrollable;
  final bool safeArea;

  @override
  Widget build(BuildContext context) {
    final window = AppWindow.of(context);
    final pad = padding ?? window.pagePadding;

    Widget body = Padding(padding: pad, child: child);
    body = Align(
      alignment: AlignmentDirectional.topCenter,
      child: ConstrainedBox(
        constraints: BoxConstraints(maxWidth: maxWidth),
        child: body,
      ),
    );
    if (scrollable) {
      body = SingleChildScrollView(
        padding: EdgeInsets.zero,
        physics: const AlwaysScrollableScrollPhysics(),
        child: body,
      );
    }
    if (safeArea) body = SafeArea(child: body);
    return body;
  }
}

/// List beside detail on expanded windows, list only below that.
///
/// The master pane keeps its own scroll position and the detail pane is built
/// lazily: on a phone [detailBuilder] is never called, so a heavy editor is not
/// even instantiated until the user opens it.
class MasterDetailPane extends StatelessWidget {
  const MasterDetailPane({
    super.key,
    required this.master,
    required this.detailBuilder,
    this.masterWidth = AppBreakpoints.masterPane,
    this.detailPlaceholder,
  });

  final Widget master;
  final WidgetBuilder detailBuilder;
  final double masterWidth;
  final Widget? detailPlaceholder;

  @override
  Widget build(BuildContext context) {
    final window = AppWindow.of(context);
    if (!window.supportsTwoPane) return master;

    return Row(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        SizedBox(
          width: masterWidth.clamp(280, window.size.width * 0.45),
          child: master,
        ),
        const VerticalDivider(width: 1),
        Expanded(
          child: ClipRect(
            child: detailPlaceholder ?? Builder(builder: detailBuilder),
          ),
        ),
      ],
    );
  }
}

/// The detail-pane frame: header strip, scrollable body, optional footer.
///
/// Used by rules and logs so both panes share one visual rhythm instead of
/// each hand-rolling a `Column` with its own padding.
class AppDetailPane extends StatelessWidget {
  const AppDetailPane({
    super.key,
    required this.title,
    this.subtitle,
    this.actions = const <Widget>[],
    required this.child,
    this.footer,
  });

  final String title;
  final String? subtitle;
  final List<Widget> actions;
  final Widget child;
  final Widget? footer;

  @override
  Widget build(BuildContext context) {
    final palette = AppColors.of(context);
    final theme = Theme.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Container(
          padding: const EdgeInsetsDirectional.fromSTEB(
            AppSpacing.lg,
            AppSpacing.md,
            AppSpacing.sm,
            AppSpacing.md,
          ),
          decoration: BoxDecoration(
            color: palette.surface2,
            border: Border(bottom: BorderSide(color: palette.border)),
          ),
          child: Row(
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(
                      title,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: theme.textTheme.titleMedium,
                    ),
                    if (subtitle != null && subtitle!.isNotEmpty)
                      Padding(
                        padding: const EdgeInsets.only(top: 2),
                        child: Text(
                          subtitle!,
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                          style: theme.textTheme.bodySmall,
                        ),
                      ),
                  ],
                ),
              ),
              const SizedBox(width: AppSpacing.sm),
              Wrap(spacing: AppSpacing.xs, children: actions),
            ],
          ),
        ),
        Expanded(child: child),
        if (footer != null)
          Container(
            padding: const EdgeInsets.all(AppSpacing.md),
            decoration: BoxDecoration(
              color: palette.surface2,
              border: Border(top: BorderSide(color: palette.border)),
            ),
            child: SafeArea(top: false, child: footer!),
          ),
      ],
    );
  }
}
