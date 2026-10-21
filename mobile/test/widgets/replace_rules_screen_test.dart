/// The text-replacement screen against a canned server.
///
/// A wiring test with teeth: it drives the real [ReplaceRulesScreen] over the
/// real `ApiClient` through [AppState.transportOverride], so the request the
/// screen builds (path, query) and the JSON it can parse are both exercised.
/// What it pins is the behaviour a user would notice if it regressed — the
/// counts header, one row per rule (not one per task copy), the enable label
/// flipping, delete asking first, and the test preview rendering the server's
/// answer instead of guessing locally.
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:provider/provider.dart';

import 'package:autoforward_mobile/core/app_state.dart';
import 'package:autoforward_mobile/core/env.dart';
import 'package:autoforward_mobile/core/storage.dart';
import 'package:autoforward_mobile/features/replace/replace_rules_screen.dart';
import 'package:autoforward_mobile/l10n/app_messages.dart';

const _env = AppEnv(
  apiBaseUrl: 'https://atf.example.com/',
  apiBaseUrlDev: 'http://10.0.2.2:8080/',
  minSupportedApiVersion: '1.0.0',
  documentationUrl: 'https://example.com/docs',
  repositoryUrl: 'https://example.com/repo',
);

/// One rule, assigned to two tasks: the server sends it once, with both ids.
Map<String, dynamic> _rule({
  String id = 'r1',
  bool enabled = true,
  String name = 'Promo cleanup',
  String to = '',
}) => {
  'id': id,
  'name': name,
  'source': 'promo',
  'to': to,
  'mode': 'literal',
  'flags': '',
  'whole_word': false,
  'enabled': enabled,
  'priority': 100,
  'task_ids': ['t1', 't2'],
  'task_names': ['News', 'Deals'],
  'version': 1,
  'updated_at': 1726000000,
  'badge': 'promo',
};

/// A transport that records every request and answers from [handler].
class _Server {
  _Server(this.handler);

  final http.Response Function(http.Request request) handler;
  final List<http.Request> calls = [];

  Future<http.Response> transport(
    Uri url, {
    required String method,
    Map<String, String>? headers,
    Object? body,
    required Duration timeout,
  }) async {
    final request = http.Request(method, url);
    if (headers != null) request.headers.addAll(headers);
    if (body != null) request.body = body is String ? body : jsonEncode(body);
    calls.add(request);
    return handler(request);
  }

  List<String> get paths => [for (final c in calls) c.url.path];
}

http.Response _json(Object payload, [int status = 200]) => http.Response(
  jsonEncode(payload),
  status,
  headers: const {'content-type': 'application/json'},
);

/// The list payload the handler builds: `counts` covers the filtered set and
/// `tasks` rides along so the editor needs no second request.
Map<String, dynamic> _page({
  List<Map<String, dynamic>>? rules,
  Map<String, dynamic>? tasks,
  int total = 1,
  int page = 1,
  int pages = 1,
  int enabled = 1,
}) => {
  'rules': rules ?? [_rule()],
  'tasks':
      tasks ??
      [
        {'id': 't1', 'name': 'News'},
        {'id': 't2', 'name': 'Deals'},
      ],
  'total': total,
  'page': page,
  'pages': pages,
  'per_page': 12,
  'counts': {'total': total, 'enabled': enabled, 'disabled': total - enabled},
  'query': '',
  'status': 'all',
  'task_id': '',
};

Widget _app(AppState state) => ChangeNotifierProvider<AppState>.value(
  value: state,
  child: const MaterialApp(
    locale: Locale('en'),
    localizationsDelegates: [
      AppMessages.delegate,
      GlobalMaterialLocalizations.delegate,
      GlobalWidgetsLocalizations.delegate,
      GlobalCupertinoLocalizations.delegate,
    ],
    supportedLocales: AppMessages.supportedLocales,
    home: ReplaceRulesScreen(),
  ),
);

AppState _stateFor(_Server server) => AppState(
  env: _env,
  tokenStore: MemoryTokenStore(),
  preferenceStore: MemoryPreferenceStore(),
  transportOverride: server.transport,
);

Future<void> _pump(WidgetTester tester, _Server server) async {
  tester.view.physicalSize = const Size(1080, 2400);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(_app(_stateFor(server)));
  await tester.pumpAndSettle();
}

void main() {
  final t = AppMessages(const Locale('en'));

  testWidgets('the header counts every rule and each row is one rule', (
    tester,
  ) async {
    final server = _Server((_) => _json(_page()));
    await _pump(tester, server);

    // Counts come from the server, not from counting the rendered rows.
    expect(
      find.text(
        t.get('rp_counts', params: {'total': '1', 'on': '1', 'off': '0'}),
      ),
      findsOneWidget,
    );
    expect(find.text('Promo cleanup'), findsOneWidget);
    // Two tasks, one row: the grouping is the whole point of the manager.
    expect(
      find.text(
        t.get('rp_runs_on', params: {'n': '2', 'names': 'News, Deals'}),
      ),
      findsOneWidget,
    );
    expect(server.paths, ['/api/replace-rules']);
    expect(server.calls.single.url.queryParameters['status'], isNull);
  });

  testWidgets('enable/disable posts the absolute state and re-reads counts', (
    tester,
  ) async {
    var enabled = true;
    final server = _Server((request) {
      if (request.method == 'POST' && request.url.path.endsWith('/enable')) {
        enabled = false;
        return _json({'rule': _rule(enabled: false)});
      }
      return _json(
        _page(
          rules: [_rule(enabled: enabled)],
          enabled: enabled ? 1 : 0,
        ),
      );
    });
    await _pump(tester, server);
    expect(find.text(t.get('rp_disable')), findsOneWidget);

    await tester.tap(find.text(t.get('rp_disable')));
    await tester.pumpAndSettle();

    final enableCall = server.calls.firstWhere(
      (c) => c.url.path.endsWith('/enable'),
    );
    expect(jsonDecode(enableCall.body), {'enabled': false});
    // The label flips because the list was re-read, not because the row was
    // patched locally: a failed write would then leave the UI lying.
    expect(find.text(t.get('rp_enable')), findsOneWidget);
    expect(
      find.text(
        t.get('rp_counts', params: {'total': '1', 'on': '0', 'off': '1'}),
      ),
      findsOneWidget,
    );
  });

  testWidgets('delete asks before sending anything', (tester) async {
    final server = _Server((request) {
      if (request.method == 'DELETE') return _json({'ok': true, 'removed': 2});
      return _json(_page());
    });
    await _pump(tester, server);

    await tester.tap(find.byTooltip(t.get('action_delete')));
    await tester.pumpAndSettle();
    expect(find.text(t.get('rp_delete_title')), findsOneWidget);
    expect(server.calls.any((c) => c.method == 'DELETE'), isFalse);

    // Cancel first: a mis-tap must not destroy a rule three tasks depend on.
    await tester.tap(find.text(t.get('action_cancel')));
    await tester.pumpAndSettle();
    expect(server.calls.any((c) => c.method == 'DELETE'), isFalse);

    await tester.tap(find.byTooltip(t.get('action_delete')));
    await tester.pumpAndSettle();
    // The confirm button is an AppButton, not a Material TextButton.
    await tester.tap(find.text(t.get('rp_delete_confirm')));
    await tester.pumpAndSettle();
    expect(server.calls.any((c) => c.method == 'DELETE'), isTrue);
  });

  testWidgets('the test preview shows the server output and match count', (
    tester,
  ) async {
    final server = _Server((request) {
      if (request.url.path.endsWith('/test')) {
        return _json({
          'ok': true,
          'input': 'big promo today',
          'output': 'big  today',
          'matches': 1,
          'changed': true,
        });
      }
      return _json(_page());
    });
    await _pump(tester, server);

    await tester.tap(find.text(t.get('rp_test')));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField).last, 'big promo today');
    await tester.tap(find.text(t.get('rp_test_run')));
    await tester.pumpAndSettle();

    final call = server.calls.firstWhere((c) => c.url.path.endsWith('/test'));
    expect(jsonDecode(call.body), {
      'rule_id': 'r1',
      'sample': 'big promo today',
    });
    expect(find.text('big  today'), findsOneWidget);
    expect(
      find.text(t.get('rp_test_matches', params: {'n': '1'})),
      findsOneWidget,
    );
  });

  testWidgets('an empty account says so and offers to create a rule', (
    tester,
  ) async {
    final server = _Server(
      (_) => _json(_page(rules: const [], total: 0, enabled: 0)),
    );
    await _pump(tester, server);

    expect(find.text(t.get('rp_empty_title')), findsOneWidget);
    expect(find.text(t.get('rp_empty_body')), findsOneWidget);
    expect(find.text(t.get('rp_new')), findsWidgets);
  });

  testWidgets('a second page moves the pager without losing the filter', (
    tester,
  ) async {
    final server = _Server((request) {
      final page = int.parse(request.url.queryParameters['page'] ?? '1');
      return _json(
        _page(
          rules: [_rule(id: 'r$page', name: 'Rule $page')],
          total: 2,
          page: page,
          pages: 2,
        ),
      );
    });
    await _pump(tester, server);
    expect(
      find.text(t.get('rp_page_of', params: {'page': '1', 'pages': '2'})),
      findsOneWidget,
    );

    await tester.tap(find.byTooltip(t.get('rp_next')));
    await tester.pumpAndSettle();

    expect(find.text('Rule 2'), findsOneWidget);
    expect(
      find.text(t.get('rp_page_of', params: {'page': '2', 'pages': '2'})),
      findsOneWidget,
    );
    expect(server.calls.last.url.queryParameters['page'], '2');
  });

  testWidgets('a search sends the query and resets to page one', (
    tester,
  ) async {
    final server = _Server((_) => _json(_page()));
    await _pump(tester, server);

    await tester.enterText(find.byType(TextField).first, '  promo  ');
    await tester.testTextInput.receiveAction(TextInputAction.search);
    await tester.pumpAndSettle();

    final last = server.calls.last.url.queryParameters;
    expect(
      last['q'],
      'promo',
      reason: 'the query is trimmed before it is sent',
    );
    expect(last['page'], isNull, reason: 'a filter change starts at page 1');
  });

  testWidgets('a server error is shown, never rendered as an empty list', (
    tester,
  ) async {
    final server = _Server((_) => _json({'error': 'boom'}, 500));
    await _pump(tester, server);

    expect(find.text(t.get('rp_empty_title')), findsNothing);
    expect(find.textContaining('boom'), findsWidgets);
  });
}
