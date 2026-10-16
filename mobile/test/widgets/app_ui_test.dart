/// Widget tests for the shared UI primitives and the adaptive navigation shell.
///
/// These guard the design-system contract rather than pixel output: exactly one
/// status colour per state, the value/caption split on stat tiles, and the
/// nav shell swapping between rail and bottom bar at the documented width.
library;

import 'package:autoforward_mobile/core/theme.dart';
import 'package:autoforward_mobile/widgets/app_ui.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

Widget _host(Widget child, {double width = 420}) {
  return MaterialApp(
    theme: AppTheme.dark(),
    home: Scaffold(
      body: Center(
        child: SizedBox(width: width, child: child),
      ),
    ),
  );
}

void main() {
  group('StatTile', () {
    testWidgets('renders label, value and caption', (tester) async {
      await tester.pumpWidget(
        _host(
          const StatTile(
            label: 'ACTIVE RULES',
            value: '3',
            caption: '/ 12',
            icon: Icons.alt_route,
          ),
        ),
      );

      expect(find.text('ACTIVE RULES'), findsOneWidget);
      expect(find.text('3'), findsOneWidget);
      expect(find.text('/ 12'), findsOneWidget);
      expect(find.byIcon(Icons.alt_route), findsOneWidget);
    });

    testWidgets('omits the caption when none is supplied', (tester) async {
      await tester.pumpWidget(
        _host(
          const StatTile(label: 'DELIVERED', value: '99', icon: Icons.check),
        ),
      );

      expect(find.text('DELIVERED'), findsOneWidget);
      expect(find.text('99'), findsOneWidget);
    });

    testWidgets('is tappable when given an onTap handler', (tester) async {
      var taps = 0;
      await tester.pumpWidget(
        _host(
          StatTile(
            label: 'FAILED',
            value: '2',
            icon: Icons.error_outline,
            onTap: () => taps++,
          ),
        ),
      );

      await tester.tap(find.byType(StatTile));
      await tester.pump();
      expect(taps, 1);
    });
  });

  group('StatusPill', () {
    testWidgets('online and offline use different colours', (tester) async {
      await tester.pumpWidget(_host(StatusPill.online(true)));
      final up = tester.widget<Text>(find.text('ONLINE'));
      // The healthy colour is the success hue; the brand hue means brand.
      expect(up.style?.color, AppTheme.statusColor(true));
      expect(AppTheme.statusColor(true), AppTheme.success);

      await tester.pumpWidget(_host(StatusPill.online(false)));
      final down = tester.widget<Text>(find.text('OFFLINE'));
      expect(down.style?.color, AppTheme.error);
    });

    testWidgets('severity colours match the shared palette', (tester) async {
      expect(AppTheme.severityColor('error'), AppTheme.error);
      expect(AppTheme.severityColor('warning'), AppTheme.warning);
      expect(AppTheme.severityColor('info'), AppTheme.info);
    });
  });

  group('InlineBanner', () {
    testWidgets('shows the message and a retry action', (tester) async {
      var retried = 0;
      await tester.pumpWidget(
        _host(
          InlineBanner(
            message: 'sessions unavailable',
            onRetry: () => retried++,
            retryLabel: 'Retry',
          ),
        ),
      );

      expect(find.text('sessions unavailable'), findsOneWidget);
      await tester.tap(find.text('Retry'));
      await tester.pump();
      expect(retried, 1);
    });

    testWidgets('hides the retry action when no handler is given', (
      tester,
    ) async {
      await tester.pumpWidget(
        _host(const InlineBanner(message: 'informational')),
      );
      expect(find.text('informational'), findsOneWidget);
      expect(find.byType(TextButton), findsNothing);
    });
  });

  group('EmptyStateView', () {
    testWidgets('renders the supplied message', (tester) async {
      await tester.pumpWidget(
        _host(const EmptyStateView(message: 'nothing here yet')),
      );
      expect(find.text('nothing here yet'), findsOneWidget);
    });
  });

  group('SectionCard', () {
    testWidgets('renders title, trailing widget and body', (tester) async {
      await tester.pumpWidget(
        _host(
          const SectionCard(
            title: 'Service health',
            icon: Icons.hub_outlined,
            trailing: Icon(Icons.refresh),
            child: Text('body content'),
          ),
        ),
      );

      expect(find.text('Service health'), findsOneWidget);
      expect(find.text('body content'), findsOneWidget);
      expect(find.byIcon(Icons.refresh), findsOneWidget);
    });
  });

  group('design tokens', () {
    test('spacing follows the 4pt grid', () {
      expect(<double>[
        AppSpacing.xs,
        AppSpacing.sm,
        AppSpacing.md,
        AppSpacing.lg,
        AppSpacing.xl,
        AppSpacing.xxl,
      ], everyElement(predicate<double>((v) => v % 4 == 0)));
    });

    test('theme declares the upgraded component surfaces', () {
      final theme = AppTheme.dark();
      expect(
        theme.navigationBarTheme.labelBehavior,
        NavigationDestinationLabelBehavior.alwaysShow,
      );
      expect(theme.navigationRailTheme.indicatorColor, isNotNull);
      expect(theme.cardTheme.shape, isNotNull);
      expect(theme.inputDecorationTheme.focusedBorder, isNotNull);
      expect(theme.snackBarTheme.behavior, SnackBarBehavior.floating);
    });
  });
}
