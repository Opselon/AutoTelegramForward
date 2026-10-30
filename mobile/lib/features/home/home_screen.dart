/// Root navigation shell: adaptive by window size class.
///
/// Eleven top-level destinations cannot fit a Material bottom bar on a phone:
/// M3 recommends three to five, and eleven equal slices are narrower than their
/// own labels (the first and last used to be clipped by the screen edges). The
/// shell therefore follows the size classes in `docs/ui-ux-system.md`:
///
///   * **compact** (`< 600 dp`) — a five-slot [NavigationBar]: the four
///     destinations an operator uses constantly plus **More**, which opens a
///     sheet with the rest. Every destination stays one tap away, every target
///     is 48 dp, and no label is ever clipped.
///   * **medium** (`600–839 dp`) — a labelled [NavigationRail] where the
///     vertical space allows all eleven labels, and icon+selected-label where a
///     landscape window is too short for them.
///   * **expanded** (`>= 840 dp`) — the same rail, and the screens behind it
///     get two panes (rules, logs) through [MasterDetailPane].
///
/// A landscape phone (short height) keeps the bottom bar: an eleven-entry rail
/// does not fit in 360 dp of height, and squeezing it is how entries become
/// unreachable. Both surfaces are generated from one destination list, so they
/// cannot drift apart, and the selected index survives a layout change.
library;

import 'package:flutter/material.dart';

import '../../core/layout.dart';
import '../../core/theme.dart';
import '../../l10n/app_messages.dart';
import '../ai/ai_screen.dart';
import '../auto_reply/auto_reply_screen.dart';
import '../chats/chats_screen.dart';
import '../dashboard/dashboard_screen.dart';
import '../delivery/delivery_screen.dart';
import '../filters/filters_screen.dart';
import '../logs/logs_screen.dart';
import '../replace/replace_rules_screen.dart';
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
  int _currentIndex = 0;

  /// Screens are created lazily on first visit. Mounting all eleven at startup
  /// fires eleven simultaneous API requests and does work for tabs the user may
  /// never open; once visited a screen is kept alive so its scroll position and
  /// loaded data survive tab switches (and a rotation into the rail layout).
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
    // Replace sits next to Rules: it answers the same traffic from the other
    // side — not "where does a message go" but "what does it say once it
    // arrives", so the two belong in the same reach.
    _Destination(
      icon: Icons.find_replace_outlined,
      selectedIcon: Icons.find_replace,
      labelKey: 'nav_replace',
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
    // Auto-reply used to be reachable only from inside the AI screen, which
    // hid a whole five-tab feature behind an unrelated destination. It is a
    // first-class surface (settings, prompts, targets, rules, activity) and
    // now sits next to the other AI capabilities at the top level.
    _Destination(
      icon: Icons.smart_toy_outlined,
      selectedIcon: Icons.smart_toy,
      labelKey: 'nav_auto_reply',
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
    ReplaceRulesScreen(),
    ChatsScreen(),
    SessionsScreen(),
    FiltersScreen(),
    AiScreen(),
    AutoReplyScreen(),
    LogsScreen(),
    DeliveryScreen(),
    SettingsScreen(),
  ];

  /// The four destinations a phone shows directly. Order is the bar's order,
  /// not the destination list's: dashboard, rules, logs, settings are the ones
  /// an operator opens every day.
  static const List<int> _compactSlots = [0, 1, 8, 10];

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
    final window = AppWindow.of(context);

    // A rail needs vertical room for eleven entries: 11 × 56 dp plus the brand
    // mark. Below that a landscape phone would lose the tail of the list, so it
    // keeps the horizontal bar instead.
    final railFits = window.size.height >= 692;
    final useRail = window.size.width >= AppBreakpoints.medium && railFits;

    final body = IndexedStack(
      index: _currentIndex,
      children: [
        for (var i = 0; i < _screens.length; i++)
          // Unvisited tabs stay as an empty placeholder: cheap to keep in the
          // stack, but nothing is built, fetched or subscribed until opened.
          if (!_visited.contains(i))
            const SizedBox.shrink()
          // Rules and logs own their width: they lay out a two-pane
          // master-detail on an expanded window and must not be capped to a
          // single readable column.
          else if (i == 1 || i == 8)
            _screens[i]
          else
            _ContentWidth(child: _screens[i]),
      ],
    );

    if (useRail) {
      return Scaffold(
        body: Row(
          children: [
            NavigationRail(
              selectedIndex: _currentIndex,
              onDestinationSelected: _select,
              // All eleven labels when the height allows; icon plus the selected
              // label on a short landscape window, where eleven labelled rows
              // would overflow.
              labelType: window.size.height >= 756
                  ? NavigationRailLabelType.all
                  : NavigationRailLabelType.selected,
              leading: const Padding(
                padding: EdgeInsets.symmetric(vertical: AppSpacing.md),
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
      bottomNavigationBar: _CompactNavigationBar(
        destinations: _destinations,
        slots: _compactSlots,
        selectedIndex: _currentIndex,
        onSelected: _select,
        onMore: () => _openMoreSheet(context),
        labelOf: (d) => t.get(d.labelKey),
      ),
    );
  }

  Future<void> _openMoreSheet(BuildContext context) async {
    final t = AppMessages.of(context);
    final secondary = [
      for (var i = 0; i < _destinations.length; i++)
        if (!_compactSlots.contains(i)) i,
    ];

    final picked = await showModalBottomSheet<int>(
      context: context,
      showDragHandle: true,
      isScrollControlled: true,
      builder: (ctx) => SafeArea(
        top: false,
        child: ConstrainedBox(
          constraints: const BoxConstraints(
            maxWidth: AppBreakpoints.contentMax,
          ),
          child: Column(
            key: const ValueKey<String>('nav_more_sheet'),
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Padding(
                padding: const EdgeInsetsDirectional.fromSTEB(
                  AppSpacing.lg,
                  AppSpacing.sm,
                  AppSpacing.lg,
                  AppSpacing.sm,
                ),
                child: Semantics(
                  header: true,
                  child: Text(
                    t.get('nav_more_title'),
                    style: Theme.of(ctx).textTheme.titleMedium,
                  ),
                ),
              ),
              for (final i in secondary)
                ListTile(
                  key: ValueKey<String>(
                    'nav_more_item_${_destinations[i].labelKey}',
                  ),
                  leading: Icon(
                    i == _currentIndex
                        ? _destinations[i].selectedIcon
                        : _destinations[i].icon,
                    color: i == _currentIndex
                        ? Theme.of(ctx).colorScheme.primary
                        : null,
                  ),
                  title: Text(t.get(_destinations[i].labelKey)),
                  selected: i == _currentIndex,
                  minVerticalPadding: AppSpacing.md,
                  onTap: () => Navigator.of(ctx).pop(i),
                ),
              const SizedBox(height: AppSpacing.sm),
            ],
          ),
        ),
      ),
    );

    if (picked != null && mounted) _select(picked);
  }
}

/// The bottom bar for compact windows: four primary destinations plus More.
///
/// Uses the real [NavigationBar], so the M3 indicator, ripple, semantics and
/// 48 dp targets come from the framework instead of a hand-rolled strip. The
/// More slot adopts the icon and label of the secondary destination that is
/// currently open, so the bar always names the page you are on.
class _CompactNavigationBar extends StatelessWidget {
  const _CompactNavigationBar({
    required this.destinations,
    required this.slots,
    required this.selectedIndex,
    required this.onSelected,
    required this.onMore,
    required this.labelOf,
  });

  final List<_Destination> destinations;
  final List<int> slots;
  final int selectedIndex;
  final ValueChanged<int> onSelected;
  final VoidCallback onMore;
  final String Function(_Destination) labelOf;

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final palette = AppColors.of(context);
    final inSlots = slots.contains(selectedIndex);
    final moreLabel = inSlots
        ? t.get('nav_more')
        : labelOf(destinations[selectedIndex]);

    return NavigationBar(
      key: const ValueKey<String>('nav_bar'),
      selectedIndex: inSlots ? slots.indexOf(selectedIndex) : slots.length,
      onDestinationSelected: (slot) {
        if (slot < slots.length) {
          onSelected(slots[slot]);
        } else {
          onMore();
        }
      },
      backgroundColor: palette.surface2,
      destinations: [
        for (final i in slots)
          NavigationDestination(
            key: ValueKey<String>('nav_dest_${destinations[i].labelKey}'),
            icon: Icon(destinations[i].icon),
            selectedIcon: Icon(destinations[i].selectedIcon),
            label: labelOf(destinations[i]),
          ),
        NavigationDestination(
          key: const ValueKey<String>('nav_dest_more'),
          icon: inSlots
              ? const Icon(Icons.more_horiz)
              : Icon(destinations[selectedIndex].selectedIcon),
          label: moreLabel,
        ),
      ],
    );
  }
}

/// Caps a single-column screen at a readable width and centres it.
///
/// Without this a tablet renders one column of forms across 1280 dp — the
/// "stretched phone UI" the UI contract forbids. The cap is a constraint, not a
/// layout branch: the screen keeps its own Scaffold and AppBar, it simply does
/// not grow past [AppBreakpoints.contentMax] + a wide-screen margin.
class _ContentWidth extends StatelessWidget {
  const _ContentWidth({required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(
      builder: (context, constraints) {
        const cap = AppBreakpoints.contentMax + 240;
        if (constraints.maxWidth <= cap) return child;
        return Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: cap),
            child: child,
          ),
        );
      },
    );
  }
}

/// Compact brand mark shown at the top of the navigation rail.
class _BrandMark extends StatelessWidget {
  const _BrandMark();

  @override
  Widget build(BuildContext context) {
    return Semantics(
      label: AppMessages.of(context).get('app_name'),
      child: Container(
        width: 40,
        height: 40,
        decoration: BoxDecoration(
          gradient: AppGradients.brand,
          borderRadius: AppRadius.allMd,
          boxShadow: AppElevation.shadows(context, level: 1),
        ),
        child: const Icon(Icons.bolt, color: Colors.white, size: 22),
      ),
    );
  }
}
