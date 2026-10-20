/// The AI auto-reply area: one entry point, five surfaces.
///
/// Settings, the prompt library, targeting, rules and delivery activity are
/// facets of one feature, so they share a shell instead of becoming five more
/// top-level destinations — the navigation rail already carries nine.
library;

import 'package:flutter/material.dart';

import '../../l10n/app_messages.dart';
import 'activity_view.dart';
import 'prompts_view.dart';
import 'rules_view.dart';
import 'settings_view.dart';
import 'targets_view.dart';

class AutoReplyScreen extends StatelessWidget {
  const AutoReplyScreen({super.key});

  /// The tabs, in the order an operator configures them: what the bot is, what
  /// it says, who it answers, when it answers, and what it actually did.
  static const List<String> _tabKeys = [
    'ar_tab_settings',
    'ar_tab_prompts',
    'ar_tab_targets',
    'ar_tab_rules',
    'ar_tab_activity',
  ];

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    return DefaultTabController(
      length: _tabKeys.length,
      child: Scaffold(
        appBar: AppBar(
          title: Text(t.get('ar_title')),
          bottom: TabBar(
            isScrollable: true,
            tabAlignment: TabAlignment.start,
            tabs: [
              for (final key in _tabKeys)
                Tab(key: ValueKey<String>('ar_tab_$key'), text: t.get(key)),
            ],
          ),
        ),
        body: const TabBarView(
          children: [
            AutoReplySettingsView(),
            AutoReplyPromptsView(),
            AutoReplyTargetsView(),
            AutoReplyRulesView(),
            AutoReplyActivityView(),
          ],
        ),
      ),
    );
  }
}
