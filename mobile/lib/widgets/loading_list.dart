/// Skeleton loading primitive.
///
/// A list that is loading shows the *shape* of its rows, not a spinner on a
/// blank page: the layout does not jump when the data lands, and the user can
/// see how many rows are coming. The pulse is one animation for the whole list
/// (a single [FadeTransition] layer, bounded rows), and it is disabled when the
/// platform asks for reduced motion.
library;

import 'package:flutter/material.dart';

import '../core/design_tokens.dart';
import '../l10n/app_messages.dart';

/// One grey block. `width` is nullable so a block can fill its row.
class SkeletonBlock extends StatelessWidget {
  const SkeletonBlock({
    super.key,
    this.width,
    required this.height,
    this.radius = AppRadius.sm,
  });

  final double? width;
  final double height;
  final double radius;

  @override
  Widget build(BuildContext context) {
    final palette = AppColors.of(context);
    return Container(
      width: width,
      height: height,
      decoration: BoxDecoration(
        color: palette.textMuted.withValues(alpha: 0.18),
        borderRadius: BorderRadius.circular(radius),
      ),
    );
  }
}

/// A skeleton list with fixed row extents.
///
/// [itemExtent] keeps the scroll metrics stable and lets the viewport build
/// only the visible rows, exactly like the real list it is standing in for.
class LoadingList extends StatefulWidget {
  const LoadingList({
    super.key,
    this.rows = 6,
    this.itemExtent = 92,
    this.padding = const EdgeInsets.all(AppSpacing.lg),
    this.showAvatar = true,
    // Default: lay the rows out eagerly. A skeleton is five to eight rows and
    // is nearly always placed inside another scroll view (a Column in a
    // ListView), where a nested viewport would be unbounded. Pass `false` only
    // when the skeleton is the sole child of a bounded, scrollable area.
    this.shrinkWrap = true,
  });

  final int rows;
  final double itemExtent;
  final EdgeInsetsGeometry padding;
  final bool showAvatar;

  /// Use inside an unbounded parent (a Column inside a scroll view): the rows
  /// are laid out eagerly instead of inside a viewport that cannot size itself.
  final bool shrinkWrap;

  @override
  State<LoadingList> createState() => _LoadingListState();
}

class _LoadingListState extends State<LoadingList>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 900),
  );

  @override
  void initState() {
    super.initState();
    _controller.repeat(reverse: true);
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final reduced = MediaQuery.maybeDisableAnimationsOf(context) ?? false;
    final label = AppMessages.of(context).get('state_loading');

    final list = widget.shrinkWrap
        ? Padding(
            padding: widget.padding,
            child: Column(
              children: [
                for (var i = 0; i < widget.rows; i++)
                  SizedBox(
                    height: widget.itemExtent,
                    child: _SkeletonRow(showAvatar: widget.showAvatar),
                  ),
              ],
            ),
          )
        : ListView.builder(
            padding: widget.padding,
            itemExtent: widget.itemExtent,
            itemCount: widget.rows,
            physics: const NeverScrollableScrollPhysics(),
            itemBuilder: (context, index) => RepaintBoundary(
              child: _SkeletonRow(showAvatar: widget.showAvatar),
            ),
          );

    if (reduced) {
      return Semantics(label: label, container: true, child: list);
    }

    return Semantics(
      label: label,
      container: true,
      child: FadeTransition(
        opacity: Tween<double>(begin: 0.45, end: 0.9).animate(
          CurvedAnimation(parent: _controller, curve: Curves.easeInOut),
        ),
        child: list,
      ),
    );
  }
}

class _SkeletonRow extends StatelessWidget {
  const _SkeletonRow({required this.showAvatar});

  final bool showAvatar;

  @override
  Widget build(BuildContext context) {
    return ExcludeSemantics(
      child: Padding(
        padding: const EdgeInsetsDirectional.only(bottom: AppSpacing.md),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (showAvatar) ...[
              const SkeletonBlock(width: 40, height: 40, radius: AppRadius.md),
              const SizedBox(width: AppSpacing.md),
            ],
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: const [
                  // 70% then 40% wide: a paragraph shape, not a grey bar.
                  FractionallySizedBox(
                    widthFactor: 0.7,
                    child: SkeletonBlock(height: 12),
                  ),
                  SizedBox(height: AppSpacing.sm),
                  FractionallySizedBox(
                    widthFactor: 0.4,
                    child: SkeletonBlock(height: 10),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}
