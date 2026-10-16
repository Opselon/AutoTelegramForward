/// The Delivery page, driven over a canned transport.
///
/// This screen makes claims about the engine's state, so the tests assert on
/// the two things that would make those claims lies:
///   * a failed `GET /api/queue` must render as a failure — never as a wall of
///     zeroes that reads like "nothing is wrong";
///   * the count printed after a purge must be *measured* (a re-read), not an
///     assumption, and a replay must actually hit the server for the job the
///     user tapped.
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
import 'package:autoforward_mobile/features/delivery/delivery_screen.dart';
import 'package:autoforward_mobile/l10n/app_messages.dart';
import 'package:autoforward_mobile/widgets/app_ui.dart';

const _env = AppEnv(
  apiBaseUrl: 'https://atf.example.com/',
  apiBaseUrlDev: 'http://10.0.2.2:8080/',
  minSupportedApiVersion: '1.0.0',
  documentationUrl: 'https://example.com/docs',
  repositoryUrl: 'https://example.com/repo',
);

const _json = {'content-type': 'application/json'};

typedef _Sent = ({String method, String path, Object? body});

const String _queueBody =
    '{"processed_total":40,"forwarded_total":31,"failed_total":2,'
    '"dedup_skipped_total":0,"filtered_total":7,"in_queue":3,'
    '"dead_lettered_total":2,"retry_total":5,"rules":['
    '{"rule_id":"rule-a","rule_name":"Alpha relay","is_active":true,'
    '"is_paused":false,"forwarded":25,"filtered":5,"errors":1,'
    '"last_forward_ts":1767225600,"last_error":"FloodWait 30s"},'
    '{"rule_id":"rule-b","rule_name":"Beta relay","is_active":true,'
    '"is_paused":true,"forwarded":6,"filtered":2,"errors":1}'
    '],"errors":[{"ts":1767225600,"rule_id":"rule-a",'
    '"category":"delivery","error_name":"ChannelPrivate",'
    '"severity":"error","detail":"target refused the forward"}]}';

/// Two dead-lettered jobs, so a purge has something to measure.
const String _dlqBody =
    '{"jobs":['
    '{"id":"dlq-1","rule_id":"rule-a","source_chat_id":"-1001111111111",'
    '"source_message_id":4711,"target_chat_id":"-1002222222222","attempts":3,'
    '"last_error":"FloodWait 30s","error_category":"rate_limit",'
    '"dead_lettered_at":1767225600},'
    '{"id":"dlq-2","rule_id":"rule-b","source_chat_id":"-1003333333333",'
    '"source_message_id":99,"target_chat_id":"-1004444444444","attempts":5,'
    '"last_error":"User banned","error_category":"forbidden",'
    '"dead_lettered_at":1767225700}'
    '],"entries":[],"total":2}';

/// A server that answers the three delivery endpoints. [emptyAfterAction] makes
/// the *next* `GET /api/dlq` report an empty backlog, which is how a replay or a
/// purge is observed to have taken effect.
HttpTransport _transport(
  List<_Sent> log, {
  bool queueOk = true,
  bool dlqOk = true,
  bool actionOk = true,
}) {
  var actioned = false;
  return (
    Uri url, {
    required String method,
    Map<String, String>? headers,
    Object? body,
    required Duration timeout,
  }) async {
    log.add((method: method, path: url.path, body: body));
    if (url.path.endsWith('/queue')) {
      if (!queueOk) {
        return http.Response(
          '{"error":"core unavailable","code":"unavailable"}',
          502,
          headers: _json,
        );
      }
      return http.Response(_queueBody, 200, headers: _json);
    }
    if (url.path.endsWith('/dlq') && method == 'GET') {
      if (!dlqOk) {
        return http.Response('{"error":"boom"}', 500, headers: _json);
      }
      if (actioned) {
        return http.Response(
          '{"jobs":[],"entries":[],"total":0}',
          200,
          headers: _json,
        );
      }
      return http.Response(_dlqBody, 200, headers: _json);
    }
    if (url.path.endsWith('/retry') && method == 'POST') {
      actioned = true;
      return http.Response(
        actionOk ? '{"success":true,"message":"requeued"}' : '{"success":false,"message":"not_found","error_code":"not_found"}',
        200,
        headers: _json,
      );
    }
    if (url.path.endsWith('/dlq') && method == 'DELETE') {
      actioned = true;
      return http.Response(
        '{"success":true,"message":"purged"}',
        200,
        headers: _json,
      );
    }
    return http.Response('{"error":"unexpected"}', 500, headers: _json);
  };
}

AppState _state(
  List<_Sent> log, {
  bool queueOk = true,
  bool dlqOk = true,
  bool actionOk = true,
}) => AppState(
  env: _env,
  tokenStore: MemoryTokenStore(),
  preferenceStore: MemoryPreferenceStore(),
  transportOverride: _transport(
    log,
    queueOk: queueOk,
    dlqOk: dlqOk,
    actionOk: actionOk,
  ),
);

Future<void> _pump(WidgetTester tester, AppState state) async {
  tester.view.physicalSize = const Size(1000, 2200);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(
    ChangeNotifierProvider<AppState>.value(
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
        home: DeliveryScreen(),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  final t = AppMessages(const Locale('en'));

  testWidgets(
    'the queue tab reports the server counters and one row per task',
    (tester) async {
      final log = <_Sent>[];
      await _pump(tester, _state(log));

      expect(find.text('31'), findsOneWidget); // forwarded
      expect(find.text('7'), findsOneWidget); // filtered
      expect(find.text('2'), findsWidgets); // failed
      expect(find.text('3'), findsOneWidget); // in queue
      expect(find.text('5'), findsOneWidget); // retried
      expect(find.text('Alpha relay'), findsOneWidget);
      expect(
        find.text(
          t.get(
            'delivery_rule_line',
            params: {'forwarded': '25', 'errors': '1'},
          ),
        ),
        findsOneWidget,
      );
      // A paused task is labelled paused even though it has live counters: it
      // will not deliver, and saying RUNNING would be wrong.
      expect(find.text(t.get('rules_status_paused')), findsOneWidget);
      expect(find.text('ChannelPrivate'), findsOneWidget);
    },
  );

  testWidgets('a failed queue read is an error banner, never zeroed counters', (
    tester,
  ) async {
    final log = <_Sent>[];
    await _pump(tester, _state(log, queueOk: false));

    expect(find.byType(InlineBanner), findsOneWidget);
    expect(find.byType(StatTile), findsNothing);
    expect(find.text('0'), findsNothing);
  });

  testWidgets('a failed dead-letter read says so instead of "nothing stuck"', (
    tester,
  ) async {
    final log = <_Sent>[];
    await _pump(tester, _state(log, dlqOk: false));

    await tester.tap(find.text(t.get('delivery_tab_dlq')));
    await tester.pumpAndSettle();

    expect(find.byType(InlineBanner), findsOneWidget);
    expect(find.text(t.get('delivery_dlq_empty')), findsNothing);
  });

  testWidgets('replaying a job hits that job and the row disappears', (
    tester,
  ) async {
    final log = <_Sent>[];
    await _pump(tester, _state(log));
    await tester.tap(find.text(t.get('delivery_tab_dlq')));
    await tester.pumpAndSettle();

    expect(find.text('rate_limit'), findsOneWidget);
    await tester.tap(
      find.byKey(const ValueKey<String>('delivery_replay_dlq-1')),
    );
    await tester.pumpAndSettle();

    expect(
      log.any((e) => e.method == 'POST' && e.path == '/api/dlq/dlq-1/retry'),
      isTrue,
      reason: 'the replay must go to the job the user tapped',
    );
    // The list was re-read, so what is shown is what the server now holds.
    expect(
      find.text(t.get('delivery_retry_ok', params: {'n': '1'})),
      findsOneWidget,
    );
    expect(find.text(t.get('delivery_dlq_empty')), findsOneWidget);
  });

  testWidgets('a refused replay is reported instead of looking successful', (
    tester,
  ) async {
    final log = <_Sent>[];
    await _pump(tester, _state(log, actionOk: false));
    await tester.tap(find.text(t.get('delivery_tab_dlq')));
    await tester.pumpAndSettle();

    await tester.tap(
      find.byKey(const ValueKey<String>('delivery_replay_dlq-1')),
    );
    await tester.pumpAndSettle();

    expect(
      find.textContaining(
        t.get('delivery_retry_failed', params: {'error': 'not_found'}),
      ),
      findsOneWidget,
    );
    // Still there, because the server still has it.
    expect(find.text('rate_limit'), findsOneWidget);
  });

  testWidgets('purging asks first, then reports the measured number removed', (
    tester,
  ) async {
    final log = <_Sent>[];
    await _pump(tester, _state(log));
    await tester.tap(find.text(t.get('delivery_tab_dlq')));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const ValueKey<String>('delivery_purge')));
    await tester.pumpAndSettle();
    expect(find.text(t.get('delivery_purge_confirm')), findsOneWidget);

    // Cancelling changes nothing.
    await tester.tap(find.text(t.get('action_cancel')));
    await tester.pumpAndSettle();
    expect(log.any((e) => e.method == 'DELETE'), isFalse);
    expect(find.text('rate_limit'), findsOneWidget);

    await tester.tap(find.byKey(const ValueKey<String>('delivery_purge')));
    await tester.pumpAndSettle();
    await tester.tap(find.text(t.get('action_confirm')));
    await tester.pumpAndSettle();

    expect(
      log.any((e) => e.method == 'DELETE' && e.path == '/api/dlq'),
      isTrue,
    );
    // Two jobs were there, two are gone: the number is a measurement.
    expect(
      find.text(t.get('delivery_purge_ok', params: {'n': '2'})),
      findsOneWidget,
    );
    expect(find.text(t.get('delivery_dlq_empty')), findsOneWidget);
  });
}
