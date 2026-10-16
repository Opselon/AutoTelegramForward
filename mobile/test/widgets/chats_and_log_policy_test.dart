/// The chat surfaces: the picker that turns routing into selection, and the
/// policy tab that explains what the log pipeline decided to collect.
///
/// Both are places where being wrong is silent rather than loud — a picker that
/// drops an entry loses a route, and a policy view that renders "0 suppressed"
/// where the server said "cannot tell" sends someone hunting for a bug that was
/// already filtered out. The tests drive the real widgets over a canned
/// transport and assert on what actually came back.
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
import 'package:autoforward_mobile/features/chats/chat_picker_sheet.dart';
import 'package:autoforward_mobile/features/logs/log_policy_tab.dart';
import 'package:autoforward_mobile/l10n/app_messages.dart';

const _env = AppEnv(
  apiBaseUrl: 'https://atf.example.com/',
  apiBaseUrlDev: 'http://10.0.2.2:8080/',
  minSupportedApiVersion: '1.0.0',
  documentationUrl: 'https://example.com/docs',
  repositoryUrl: 'https://example.com/repo',
);

const _json = {'content-type': 'application/json'};

typedef _Sent = ({String method, String path, Object? body});

/// A server that lists two chats, and can be told to behave like one that
/// predates the chat-list endpoint.
HttpTransport _transport(
  List<_Sent> log, {
  bool chatsSupported = true,
  int diagnosticsStatus = 200,
}) =>
    (
      Uri url, {
      required String method,
      Map<String, String>? headers,
      Object? body,
      required Duration timeout,
    }) async {
      log.add((method: method, path: url.path, body: body));
      if (url.path.endsWith('/chats')) {
        if (!chatsSupported) {
          return http.Response('{"error":"not found","code":"not_found"}', 404);
        }
        return http.Response(
          '{"session_id":"s-1","chats":['
          '{"chat_id":"-1001111111111","title":"Alpha News",'
          '"username":"alpha_news","chat_type":"channel","members":12000},'
          '{"chat_id":"-1002222222222","title":"Beta Group",'
          '"chat_type":"supergroup","members":50,"is_forum":true}'
          '],"count":2,"total":2}',
          200,
          headers: _json,
        );
      }
      if (url.path.endsWith('/logs/policy')) {
        return http.Response(
          '{"generated_at":"2026-01-01T00:00:00Z","scopes":{'
          '"api":{"available":true,"scope":"atf-api producer policy",'
          '"process":"atf-api","effective_levels":["info","warning","error"],'
          '"configured_levels":["info","warning","error"],'
          '"diagnostic_active":false,'
          '"counters":{"emitted":42,"suppressed_level":7,"suppressed_rate":0},'
          '"counter_scope":"process-local to atf-api since its last start",'
          '"observed_at":"2026-01-01T00:00:00Z"},'
          '"core":{"available":false,"scope":"atf-core producer policy",'
          '"process":"atf-core","reason":"no policy RPC yet",'
          '"observed_at":"2026-01-01T00:00:00Z"}},'
          '"notes":["counters are process-local"]}',
          200,
          headers: _json,
        );
      }
      if (url.path.endsWith('/logs/health')) {
        return http.Response(
          '{"status":"healthy","services":['
          '{"service":"atf-logger","status":"healthy","detail":"reachable",'
          '"data_age_seconds":12,"observed_at":"2026-01-01T00:00:00Z"}]}',
          200,
          headers: _json,
        );
      }
      if (url.path.contains('/logs/diagnostics/')) {
        if (diagnosticsStatus != 200) {
          return http.Response('{"error":"admin only"}', diagnosticsStatus);
        }
        return http.Response('{"ok":true,"seconds":600}', 200, headers: _json);
      }
      return http.Response('{"error":"unexpected"}', 500, headers: _json);
    };

AppState _state(List<_Sent> log, {bool chatsSupported = true}) => AppState(
  env: _env,
  tokenStore: MemoryTokenStore(),
  preferenceStore: MemoryPreferenceStore(),
  transportOverride: _transport(log, chatsSupported: chatsSupported),
);

AppState _policyState(List<_Sent> log, {int diagnosticsStatus = 200}) =>
    AppState(
      env: _env,
      tokenStore: MemoryTokenStore(),
      preferenceStore: MemoryPreferenceStore(),
      transportOverride: _transport(log, diagnosticsStatus: diagnosticsStatus),
    );

Widget _app(AppState state, Widget home) =>
    ChangeNotifierProvider<AppState>.value(
      value: state,
      child: MaterialApp(
        locale: const Locale('en'),
        localizationsDelegates: const [
          AppMessages.delegate,
          GlobalMaterialLocalizations.delegate,
          GlobalWidgetsLocalizations.delegate,
          GlobalCupertinoLocalizations.delegate,
        ],
        supportedLocales: AppMessages.supportedLocales,
        home: home,
      ),
    );

Finder _key(String key) => find.byKey(ValueKey<String>(key));

/// Opens the picker exactly the way the rule editor does — through the helper
/// that returns ids, so the tests assert on the real return value.
Future<void> _openPicker(
  WidgetTester tester,
  AppState state, {
  required List<String?> captured,
  Set<String> alreadySelected = const {},
}) async {
  tester.view.physicalSize = const Size(1000, 2400);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);

  await tester.pumpWidget(
    _app(
      state,
      Scaffold(
        body: Builder(
          builder: (ctx) => TextButton(
            onPressed: () async {
              final picked = await showChatPickerSheet(
                ctx,
                title: 'Select sources',
                sessionId: 's-1',
                alreadySelected: alreadySelected,
              );
              if (picked != null) captured.addAll(picked);
            },
            child: const Text('open picker'),
          ),
        ),
      ),
    ),
  );
  await tester.pump();
  await tester.tap(find.text('open picker'));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('the picker returns the confirmed selection, including entries '
      'that were already there', (tester) async {
    final log = <_Sent>[];
    final captured = <String?>[];
    await _openPicker(
      tester,
      _state(log),
      captured: captured,
      alreadySelected: {'-1009999999999'},
    );

    // The chat list arrived, and the pre-existing entry is already counted.
    expect(_key('chat_row_-1001111111111'), findsOneWidget);
    expect(find.text('-1009999999999'), findsOneWidget);

    await tester.tap(_key('chat_row_-1001111111111'));
    await tester.pumpAndSettle();
    await tester.tap(_key('chat_picker_confirm'));
    await tester.pumpAndSettle();

    expect(captured, containsAll(<String>['-1001111111111', '-1009999999999']));
    expect(captured, hasLength(2));
  });

  testWidgets('un-ticking a chat removes it from the result, so a pick that '
      'looks like a removal really is one', (tester) async {
    final log = <_Sent>[];
    final captured = <String?>[];
    await _openPicker(
      tester,
      _state(log),
      captured: captured,
      alreadySelected: {'-1001111111111'},
    );

    await tester.tap(_key('chat_row_-1001111111111'));
    await tester.pumpAndSettle();
    await tester.tap(_key('chat_picker_confirm'));
    await tester.pumpAndSettle();

    expect(captured, isEmpty);
  });

  testWidgets('a hand-typed duplicate is refused instead of silently '
      'overwriting a picked chat', (tester) async {
    final log = <_Sent>[];
    final captured = <String?>[];
    // The chat was picked already (re-opening the sheet to review a route is the
    // normal way this happens); typing the same id again must not add a second
    // entry for it.
    await _openPicker(
      tester,
      _state(log),
      captured: captured,
      alreadySelected: {'-1001111111111'},
    );

    await tester.enterText(_key('chat_picker_manual'), '-1001111111111');
    await tester.tap(_key('chat_picker_manual_add'));
    await tester.pumpAndSettle();

    expect(
      find.text(AppMessages(const Locale('en')).get('chats_picker_duplicate')),
      findsOneWidget,
    );

    await tester.tap(_key('chat_picker_manual_add'));
    await tester.pumpAndSettle();
    // The count is what matters: the picked id is there exactly once, so the
    // second attempt added nothing.
    expect(
      find.text(
        AppMessages(const Locale('en'))
            .get('chats_picker_selected_count', params: {'n': '1'}),
      ),
      findsOneWidget,
    );

    await tester.tap(_key('chat_picker_confirm'));
    await tester.pumpAndSettle();
    expect(captured, <String?>['-1001111111111']);
  });

  testWidgets(
    'a server that cannot list chats still lets the user type an id',
    (tester) async {
      final log = <_Sent>[];
      final captured = <String?>[];
      await _openPicker(
        tester,
        _state(log, chatsSupported: false),
        captured: captured,
      );

      expect(
        find.text(
          AppMessages(const Locale('en')).get('chats_picker_unavailable'),
        ),
        findsOneWidget,
      );

      await tester.enterText(_key('chat_picker_manual'), '@alpha_news');
      await tester.tap(_key('chat_picker_manual_add'));
      await tester.pumpAndSettle();
      await tester.tap(_key('chat_picker_confirm'));
      await tester.pumpAndSettle();

      expect(captured, <String>['@alpha_news']);
    },
  );

  testWidgets('the policy tab states an unreadable scope as a reason, not as '
      'zero suppressed records', (tester) async {
    final log = <_Sent>[];
    tester.view.physicalSize = const Size(1000, 2400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await tester.pumpWidget(
      _app(_policyState(log), const Scaffold(body: LogPolicyTab())),
    );
    await tester.pumpAndSettle();

    final t = AppMessages(const Locale('en'));
    // The readable scope reports its levels and its counters.
    expect(find.textContaining('info, warning, error'), findsWidgets);
    expect(find.textContaining('42'), findsOneWidget);
    // The unreadable one reports the server's reason instead of zeros.
    expect(find.textContaining('no policy RPC yet'), findsOneWidget);
    expect(t.get('logs_policy_diag_off'), isNotEmpty);
    // And the health line is the populated one, not the empty-state text.
    expect(find.textContaining('12'), findsWidgets);
  });

  testWidgets('a non-admin gets the admin-required message, not a permission '
      'code', (tester) async {
    final log = <_Sent>[];
    tester.view.physicalSize = const Size(1000, 2400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await tester.pumpWidget(
      _app(
        _policyState(log, diagnosticsStatus: 403),
        const Scaffold(body: LogPolicyTab()),
      ),
    );
    await tester.pumpAndSettle();

    final t = AppMessages(const Locale('en'));
    await tester.tap(find.text(t.get('logs_policy_start')));
    await tester.pumpAndSettle();

    expect(find.text(t.get('logs_policy_admin_required')), findsOneWidget);
  });
}
