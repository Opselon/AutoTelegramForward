/// Root navigation screen containing the bottom navigation bar.
library;

import 'package:flutter/material.dart';

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

class _HomeScreenState extends State<HomeScreen> {
  int _currentIndex = 0;

  static const List<Widget> _screens = [
    DashboardScreen(),
    RulesScreen(),
    SessionsScreen(),
    FiltersScreen(),
    AiScreen(),
    LogsScreen(),
    SettingsScreen(),
  ];

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);

    return Scaffold(
      body: IndexedStack(
        index: _currentIndex,
        children: _screens,
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _currentIndex,
        onDestinationSelected: (idx) => setState(() => _currentIndex = idx),
        destinations: [
          NavigationDestination(
            icon: const Icon(Icons.dashboard_outlined),
            selectedIcon: const Icon(Icons.dashboard),
            label: t.get('nav_dashboard'),
          ),
          NavigationDestination(
            icon: const Icon(Icons.alt_route_outlined),
            selectedIcon: const Icon(Icons.alt_route),
            label: t.get('nav_rules'),
          ),
          NavigationDestination(
            icon: const Icon(Icons.send_outlined),
            selectedIcon: const Icon(Icons.send),
            label: t.get('nav_sessions'),
          ),
          NavigationDestination(
            icon: const Icon(Icons.filter_alt_outlined),
            selectedIcon: const Icon(Icons.filter_alt),
            label: t.get('nav_filters'),
          ),
          NavigationDestination(
            icon: const Icon(Icons.psychology_outlined),
            selectedIcon: const Icon(Icons.psychology),
            label: t.get('nav_ai'),
          ),
          NavigationDestination(
            icon: const Icon(Icons.history_outlined),
            selectedIcon: const Icon(Icons.history),
            label: t.get('nav_logs'),
          ),
          NavigationDestination(
            icon: const Icon(Icons.settings_outlined),
            selectedIcon: const Icon(Icons.settings),
            label: t.get('nav_settings'),
          ),
        ],
      ),
    );
  }
}
