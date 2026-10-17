/// Root navigation shell.
///
/// Nine top-level destinations do not fit a Material bottom navigation bar on
/// a phone — labels collide and targets drop below the 48dp touch minimum. So
/// the shell is adaptive:
///   * wide windows (>= 900dp, tablet / desktop / landscape) get a labelled
///     [NavigationRail] down the side, where nine entries fit comfortably;
///   * narrow windows get a bottom [NavigationBar] that drops to icons with the
///     selected label, keeping every target tappable at nine entries.
/// Both surfaces are generated from one destination list so they can never
/// drift apart, and the selected index is preserved across layout changes.
library;

import 'package:flutter/material.dart';

import '../../core/theme.dart';
import '../../l10n/app_messages.dart';
import '../ai/ai_screen.dart';
import '../chats/chats_screen.dart';
import '../dashboard/dashboard_screen.dart';
import '../delivery/delivery_screen.dart';
import '../filters/filters_screen.dart';
import '../logs/logs_screen.dart';
import '../rules/rules_screen.dart';
import '../settings/settings_screen.dart';
import '../telegram_sessions/sessions_screen.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

/// One navigable destination: its icon pair and its localized label key.
class _Destination {
  const _Destination({
    required this.icon,
    required this.selectedIcon,
    required this.labelKey,
  });

  final IconData icon;
  final IconData selectedIcon;
  final String labelKey;
}

class _HomeScreenState extends State<HomeScreen> {
  static const int _railBreakpoint = 900;

  int _currentIndex = 0;

  /// Screens are created lazily on first visit. Mounting all seven at startup
  /// fires seven simultaneous API requests and does work for tabs the user may
  /// never open; once visited a screen is kept alive so its scroll position and
  /// loaded data survive tab switches.
  final Set<int> _visited = {0};

  static const List<_Destination> _destinations = [
    _Destination(
      icon: Icons.dashboard_outlined,
      selectedIcon: Icons.dashboard,
      labelKey: 'nav_dashboard',
    ),
    _Destination(
      icon: Icons.alt_route_outlined,
      selectedIcon: Icons.alt_route,
      labelKey: 'nav_rules',
    ),
    // Chats sits next to Rules on purpose: it answers the question Rules raises
    // ("which chat is this id?"), so the two belong in the same reach.
    _Destination(
      icon: Icons.forum_outlined,
      selectedIcon: Icons.forum,
      labelKey: 'nav_chats',
    ),
    _Destination(
      icon: Icons.send_outlined,
      selectedIcon: Icons.send,
      labelKey: 'nav_sessions',
    ),
    _Destination(
      icon: Icons.filter_alt_outlined,
      selectedIcon: Icons.filter_alt,
      labelKey: 'nav_filters',
    ),
    _Destination(
      icon: Icons.psychology_outlined,
      selectedIcon: Icons.psychology,
      labelKey: 'nav_ai',
    ),
    _Destination(
      icon: Icons.history_outlined,
      selectedIcon: Icons.history,
      labelKey: 'nav_logs',
    ),
    // Delivery sits next to Logs: both answer "what happened to my traffic",
    // and this one answers the part the log stream cannot — what is still
    // pending, and what the engine has given up on.
    _Destination(
      icon: Icons.local_shipping_outlined,
      selectedIcon: Icons.local_shipping,
      labelKey: 'nav_delivery',
    ),
    _Destination(
      icon: Icons.settings_outlined,
      selectedIcon: Icons.settings,
      labelKey: 'nav_settings',
    ),
  ];

  static const List<Widget> _screens = [
    DashboardScreen(),
    RulesScreen(),
    ChatsScreen(),
    SessionsScreen(),
    FiltersScreen(),
    AiScreen(),
    LogsScreen(),
    DeliveryScreen(),
    SettingsScreen(),
  ];

  void _select(int idx) {
    if (idx == _currentIndex) return;
    setState(() {
      _visited.add(idx);
      _currentIndex = idx;
    });
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final wide = MediaQuery.sizeOf(context).width >= _railBreakpoint;
    final body = IndexedStack(
      index: _currentIndex,
      children: [
        for (var i = 0; i < _screens.length; i++)
          // Unvisited tabs stay as an empty placeholder: cheap to keep in the
          // stack, but nothing is built, fetched or subscribed until opened.
          if (_visited.contains(i)) _screens[i] else const SizedBox.shrink(),
      ],
    );

    if (wide) {
      return Scaffold(
        body: Row(
          children: [
            NavigationRail(
              selectedIndex: _currentIndex,
              onDestinationSelected: _select,
              labelType: NavigationRailLabelType.all,
              leading: const Padding(
                padding: EdgeInsets.symmetric(vertical: AppSpacing.lg),
                child: _BrandMark(),
              ),
              destinations: [
                for (final d in _destinations)
                  NavigationRailDestination(
                    icon: Icon(d.icon),
                    selectedIcon: Icon(d.selectedIcon),
                    label: Text(t.get(d.labelKey)),
                  ),
              ],
            ),
            const VerticalDivider(width: 1),
            Expanded(child: body),
          ],
        ),
      );
    }

    return Scaffold(
      body: body,
      bottomNavigationBar: _ScrollableDestinationBar(
        destinations: _destinations,
        selectedIndex: _currentIndex,
        onSelected: _select,
        labelOf: (d) => t.get(d.labelKey),
      ),
    );
  }
}

/// The bottom bar for narrow windows.
///
/// Nine labelled destinations do not fit a phone's width, and Material's
/// [NavigationBar] splits the bar into nine equal slices — narrower than the
/// labels themselves — so its row overflowed the screen and the first and last
/// labels were clipped by the edges. Every destination here keeps a fixed,
/// tappable width and the strip scrolls horizontally instead of clipping, and
/// the selected destination is scrolled into view so the active page can never
/// sit off-screen.
class _ScrollableDestinationBar extends StatefulWidget {
  const _ScrollableDestinationBar({
    required this.destinations,
    required this.selectedIndex,
    required this.onSelected,
    required this.labelOf,
  });

  final List<_Destination> destinations;
  final int selectedIndex;
  final ValueChanged<int> onSelected;
  final String Function(_Destination) labelOf;

  /// Wide enough for the longest shipped label on one line, and taller than the
  /// 48dp touch minimum.
  static const double itemWidth = 84;
  static const double barHeight = 68;

  @override
  State<_ScrollableDestinationBar> createState() =>
      _ScrollableDestinationBarState();
}

class _ScrollableDestinationBarState extends State<_ScrollableDestinationBar> {
  final ScrollController _controller = ScrollController();
  late List<GlobalKey> _itemKeys;

  @override
  void initState() {
    super.initState();
    _itemKeys = List.generate(widget.destinations.length, (_) => GlobalKey());
  }

  @override
  void didUpdateWidget(covariant _ScrollableDestinationBar oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.selectedIndex != widget.selectedIndex) _revealSelected();
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  /// Bring the active destination into view — the case that matters is a
  /// rotation from a wide window into a narrow one with a late destination
  /// selected. `Scrollable.ensureVisible` is used instead of arithmetic on the
  /// scroll offset because it is direction-aware, and in an RTL locale the
  /// strip starts from the right edge.
  void _revealSelected() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final ctx = _itemKeys[widget.selectedIndex].currentContext;
      if (ctx == null) return;
      Scrollable.ensureVisible(
        ctx,
        alignment: 0.5,
        duration: const Duration(milliseconds: 220),
        curve: Curves.easeOut,
      );
    });
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Material(
      color: theme.colorScheme.surface,
      child: SafeArea(
        top: false,
        child: Container(
          height: _ScrollableDestinationBar.barHeight,
          decoration: BoxDecoration(
            border: Border(
              top: BorderSide(color: theme.dividerColor, width: 1),
            ),
          ),
          child: SingleChildScrollView(
            key: const ValueKey<String>('nav_bar_scroll'),
            controller: _controller,
            scrollDirection: Axis.horizontal,
            padding: const EdgeInsets.symmetric(horizontal: AppSpacing.xs),
            child: Row(
              children: [
                for (var i = 0; i < widget.destinations.length; i++)
                  SizedBox(
                    key: _itemKeys[i],
                    width: _ScrollableDestinationBar.itemWidth,
                    child: _DestinationButton(
                      destination: widget.destinations[i],
                      label: widget.labelOf(widget.destinations[i]),
                      selected: i == widget.selectedIndex,
                      onTap: () => widget.onSelected(i),
                    ),
                  ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// One destination in the narrow-window bar.
class _DestinationButton extends StatelessWidget {
  const _DestinationButton({
    required this.destination,
    required this.label,
    required this.selected,
    required this.onTap,
  });

  final _Destination destination;
  final String label;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final color = selected
        ? theme.colorScheme.primary
        : theme.colorScheme.onSurfaceVariant;
    return Semantics(
      button: true,
      selected: selected,
      label: label,
      child: Tooltip(
        message: label,
        child: InkWell(
          key: ValueKey<String>('nav_item_${destination.labelKey}'),
          onTap: onTap,
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Container(
                padding: const EdgeInsets.symmetric(
                  horizontal: AppSpacing.md,
                  vertical: AppSpacing.xs,
                ),
                decoration: BoxDecoration(
                  color: selected
                      ? color.withValues(alpha: 0.16)
                      : Colors.transparent,
                  borderRadius: AppRadii.allMd,
                ),
                child: Icon(
                  selected ? destination.selectedIcon : destination.icon,
                  size: 22,
                  color: color,
                ),
              ),
              const SizedBox(height: 2),
              // One line, and never wrapped mid-word: the width is fixed, so a
              // label too long for it is ellipsised rather than clipped.
              Text(
                label,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                textAlign: TextAlign.center,
                style: theme.textTheme.labelSmall?.copyWith(
                  color: color,
                  fontWeight: selected ? FontWeight.w700 : FontWeight.w500,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// Compact brand mark shown at the top of the navigation rail.
class _BrandMark extends StatelessWidget {
  const _BrandMark();

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 40,
      height: 40,
      decoration: BoxDecoration(
        gradient: AppGradients.brand,
        borderRadius: AppRadii.allMd,
        boxShadow: AppShadows.dark(Theme.of(context).brightness),
      ),
      child: const Icon(Icons.bolt, color: Colors.white, size: 22),
    );
  }
}
