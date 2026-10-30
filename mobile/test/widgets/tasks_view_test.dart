/// The Operations tab (durable tasks), driven over a canned transport.
///
/// The tasks list only claims what the server reports, so the tests pin the
/// things that would make it lie:
///   * a failed `GET /api/tasks` renders as a failure, never as an empty list;
///   * cancel and retry hit the exact task endpoints for the task tapped, and
///     the list is re-read afterwards instead of mutating locally;
///   * the localized status pill matches the task's real status.
library;

import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:provider/provider.dart';

import 'package:autoforward_mobile/core/api_client.dart';
import 'package:autoforward_mobile/core/app_state.dart';
import 'package:autoforward_mobile/core/env.dart';
import 'package:autoforward_mobile/core/storage.dart';
import 'package:autoforward_mobile/features/delivery/tasks_view.dart';
import 'package:autoforward_mobile/l10n/app_messages.dart';
import 'package:autoforward_mobile/widgets/error_state.dart';

const _env = AppEnv(
  apiBaseUrl: 'https://atf.example.com/',
  apiBaseUrlDev: 'http://10.0.2.2:8080/',
  minSupportedApiVersion: '1.0.0',
  documentationUrl: 'https://example.com/docs',
  repositoryUrl: 'https://example.com/repo',
);

const _json = {'content-type': 'application/json'};

typedef _Sent = ({String method, String path, Object? body});

const String _tasksBody =
    '{"tasks":['
    '{"id":"t-1","kind":"session.backup","status":"RUNNING","attempt":1,'
    '"max_attempts":3,"created_at":1767225600},'
    '{"id":"t-2","kind":"backup.export","status":"FAILED","attempt":3,'
    '"max_attempts":3,"error":"core unavailable",'
    '"created_at":1767225600,"finished_at":1767229200}'
    '],"total":2}';

const String _emptyBody = '{"tasks":[],"total":0}';

/// A server that answers `/api/tasks` with [tasksBody] and records every
/// request; the first POST to a cancel/retry endpoint switches the reply to
/// [afterActionBody], which is how the mandatory re-read is observed.
HttpTransport _transport(
  List<_Sent> log, {
  String tasksBody = _tasksBody,
  String afterActionBody = _tasksBody,
  bool ok = true,
}) {
  var acted = false;
  return (
    Uri url, {
    required String method,
    Map<String, String>? headers,
    Object? body,
    required Duration timeout,
  }) async {
    log.add((method: method, path: url.path, body: body));
    if (!url.path.contains('/api/tasks')) {
      return http.Response('{"error":"not found"}', 404, headers: _json);
    }
    if (method == 'GET') {
      if (!ok) {
        return http.Response(
          '{"error":"core unavailable","code":"unavailable"}',
          502,
          headers: _json,
        );
      }
      return http.Response(acted ? afterActionBody : tasksBody, 200,
          headers: _json);
    }
    acted = true;
    return http.Response('{"task":{"id":"x","status":"CANCELLED"}}', 200,
        headers: _json);
  };
}

Future<AppState> _state(HttpTransport transport) async {
  final s = AppState(
    env: _env,
    tokenStore: MemoryTokenStore(),
    preferenceStore: MemoryPreferenceStore(),
    transportOverride: transport,
  );
  await s.restore();
  return s;
}

Widget _host(AppState state) {
  return ChangeNotifierProvider<AppState>.value(
    value: state,
    child: MaterialApp(
      theme: ThemeData(useMaterial3: true),
      locale: const Locale('en'),
      localizationsDelegates: const [
        AppMessages.delegate,
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
      supportedLocales: AppMessages.supportedLocales,
      home: const Scaffold(body: TasksView()),
    ),
  );
}

Finder _key(String key) => find.byKey(ValueKey<String>(key));

void main() {
  testWidgets('lists tasks with their kind and status pill', (tester) async {
    final log = <_Sent>[];
    await tester.pumpWidget(_host(await _state(_transport(log))));
    await tester.pumpAndSettle();

    final t = AppMessages(const Locale('en'));
    expect(find.text(t.get('tasks_kind_session_backup')), findsOneWidget);
    expect(find.text(t.get('tasks_kind_backup_export')), findsOneWidget);
    expect(find.text(t.get('tasks_status_running')), findsOneWidget);
    expect(find.text(t.get('tasks_status_failed')), findsOneWidget);
    // The failed task shows the server's error, not a sanitised blank.
    expect(find.text('core unavailable'), findsOneWidget);
  });

  testWidgets('cancelling a running task posts to its cancel endpoint and '
      're-reads the list', (tester) async {
    final log = <_Sent>[];
    await tester.pumpWidget(_host(await _state(_transport(log))));
    await tester.pumpAndSettle();

    await tester.tap(_key('task_cancel_t-1'));
    await tester.pumpAndSettle();

    expect(
      log.any(
        (s) => s.method == 'POST' && s.path.endsWith('/api/tasks/t-1/cancel'),
      ),
      isTrue,
      reason: 'cancel must hit POST /api/tasks/{id}/cancel',
    );
    // The re-read after the action is what keeps the list honest.
    expect(
      log.where((s) => s.method == 'GET' && s.path.endsWith('/api/tasks')),
      hasLength(greaterThanOrEqualTo(2)),
    );
  });

  testWidgets('a failed task offers retry, not cancel', (tester) async {
    final log = <_Sent>[];
    await tester.pumpWidget(_host(await _state(_transport(log))));
    await tester.pumpAndSettle();

    expect(_key('task_retry_t-2'), findsOneWidget);
    expect(_key('task_cancel_t-2'), findsNothing);

    await tester.tap(_key('task_retry_t-2'));
    await tester.pumpAndSettle();
    expect(
      log.any(
        (s) => s.method == 'POST' && s.path.endsWith('/api/tasks/t-2/retry'),
      ),
      isTrue,
    );
  });

  testWidgets('a failed read renders as a failure, never as "no tasks"', (
    tester,
  ) async {
    final log = <_Sent>[];
    await tester.pumpWidget(
      _host(await _state(_transport(log, ok: false))),
    );
    await tester.pumpAndSettle();

    expect(find.byType(ErrorState), findsOneWidget);
    expect(find.textContaining('core unavailable'), findsOneWidget);
  });

  testWidgets('an empty account shows the honest empty state', (tester) async {
    final log = <_Sent>[];
    await tester.pumpWidget(
      _host(await _state(_transport(log, tasksBody: _emptyBody))),
    );
    await tester.pumpAndSettle();

    expect(
      find.text(AppMessages(const Locale('en')).get('tasks_empty')),
      findsOneWidget,
    );
  });
}
