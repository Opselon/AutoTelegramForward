/// Root navigation shell.
///
/// Seven top-level destinations do not fit a Material bottom navigation bar on
/// a phone — labels collide and targets drop below the 48dp touch minimum. So
/// the shell is adaptive:
///   * wide windows (>= 900dp, tablet / desktop / landscape) get a labelled
///     [NavigationRail] down the side, where seven entries fit comfortably;
///   * narrow windows get a bottom [NavigationBar] showing icons with a short
///     label, keeping every target tappable.
/// Both surfaces are generated from one destination list so they can never
/// drift apart, and the selected index is preserved across layout changes.
library;

import 'package:flutter/material.dart';

import '../../core/theme.dart';
import '../../l10n/app_messages.dart';
import '../ai/ai_screen.dart';
import '../dashboard/dashboard_screen.dart';
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
    _Destination(
      icon: Icons.settings_outlined,
      selectedIcon: Icons.settings,
      labelKey: 'nav_settings',
    ),
  ];

  static const List<Widget> _screens = [
    DashboardScreen(),
    RulesScreen(),
    SessionsScreen(),
    FiltersScreen(),
    AiScreen(),
    LogsScreen(),
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
      bottomNavigationBar: NavigationBar(
        selectedIndex: _currentIndex,
        onDestinationSelected: _select,
        destinations: [
          for (final d in _destinations)
            NavigationDestination(
              icon: Icon(d.icon),
              selectedIcon: Icon(d.selectedIcon),
              label: t.get(d.labelKey),
              tooltip: t.get(d.labelKey),
            ),
        ],
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
