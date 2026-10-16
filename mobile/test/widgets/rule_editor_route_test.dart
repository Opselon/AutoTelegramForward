/// The rule editor drives a task's source LIST and target LIST.
///
/// A routing task is one rule with the sources and targets on it, so the editor
/// has to show every stored entry, send both lists on save, and refuse a route
/// whose ids repeat or point at each other. The tests drive the real screen with
/// a canned transport injected through `AppState.transportOverride`, and assert
/// on the request body the save actually produced.
library;

import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:provider/provider.dart';

import 'package:autoforward_mobile/core/api_client.dart';
import 'package:autoforward_mobile/core/app_state.dart';
import 'package:autoforward_mobile/core/env.dart';
import 'package:autoforward_mobile/core/models.dart';
import 'package:autoforward_mobile/core/storage.dart';
import 'package:autoforward_mobile/features/rules/rule_editor_screen.dart';
import 'package:autoforward_mobile/l10n/app_messages.dart';

const _env = AppEnv(
  apiBaseUrl: 'https://atf.example.com/',
  apiBaseUrlDev: 'http://10.0.2.2:8080/',
  minSupportedApiVersion: '1.0.0',
  documentationUrl: 'https://example.com/docs',
  repositoryUrl: 'https://example.com/repo',
);

const _json = {'content-type': 'application/json'};

/// One request the screen made, so a test can assert on what it sent.
typedef _Sent = ({String method, String path, Object? body});

/// A canned server: sessions and AI configs for the form's pickers, and an
/// echo rule for the write endpoints.
HttpTransport _transport(List<_Sent> log) => (
  Uri url, {
  required String method,
  Map<String, String>? headers,
  Object? body,
  required Duration timeout,
}) async {
  log.add((method: method, path: url.path, body: body));
  if (url.path.endsWith('/api/sessions')) {
    return http.Response(
      '{"sessions":[{"id":"s-1","is_active":true,"phone_number":"+10000000000"}]}',
      200,
      headers: _json,
    );
  }
  if (url.path.endsWith('/api/ai-configs')) {
    return http.Response('{"configs":[]}', 200, headers: _json);
  }
  return http.Response(
    '{"id":"r-saved","source_chat_id":"-1001","target_chat_id":"-1002",'
    '"is_enabled":true}',
    200,
    headers: _json,
  );
};

AppState _state(List<_Sent> log) => AppState(
  env: _env,
  tokenStore: MemoryTokenStore(),
  preferenceStore: MemoryPreferenceStore(),
  transportOverride: _transport(log),
);

/// A task with two sources and two targets, as the server stores it.
ForwardRule _twoByTwo() => const ForwardRule(
  id: 'abcdefgh-1111-2222-3333',
  sourceChatId: '-1001111111111',
  sourceChatIds: ['-1001111111111', '-1002222222222'],
  targetChatId: '-1003333333333',
  targetChatIds: ['-1003333333333', '-1004444444444'],
  sessionId: 's-1',
  isEnabled: true,
  version: 3,
);

Finder _field(String key) => find.byKey(ValueKey<String>(key));

Finder _chip(String value) => find.byKey(ValueKey<String>('route_chip_$value'));

String _fieldText(WidgetTester tester, String key) =>
    tester.widget<TextFormField>(_field(key)).controller!.text;

/// Boots the app and pushes the editor the way the rules list does.
Future<void> _pumpEditor(
  WidgetTester tester,
  AppState state, {
  ForwardRule? rule,
}) async {
  tester.view.physicalSize = const Size(1000, 2600);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);

  await tester.pumpWidget(
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
        home: Scaffold(
          body: Builder(
            builder:
                (ctx) => TextButton(
                  onPressed:
                      () => Navigator.of(ctx).push(
                        MaterialPageRoute<bool>(
                          builder: (_) => RuleEditorScreen(rule: rule),
                        ),
                      ),
                  child: const Text('open editor'),
                ),
          ),
        ),
      ),
    ),
  );
  // The localisation delegates resolve one frame after the first pump, so the
  // home route's content exists only from the second frame on.
  await tester.pump();
  await tester.tap(find.text('open editor'));
  await tester.pumpAndSettle();
}

Future<void> _tapSave(WidgetTester tester) async {
  await tester.tap(find.byTooltip('Save'));
  await tester.pumpAndSettle();
}

/// Write requests only — the reads the form makes on open are not a save.
Iterable<_Sent> _writes(List<_Sent> log) =>
    log.where((c) => c.method == 'POST' || c.method == 'PUT');

void main() {
  testWidgets('a stored 2-source / 2-target task shows all four chips', (
    tester,
  ) async {
    final log = <_Sent>[];
    await _pumpEditor(tester, _state(log), rule: _twoByTwo());

    expect(find.byType(InputChip), findsNWidgets(4));
    for (final id in const [
      '-1001111111111',
      '-1002222222222',
      '-1003333333333',
      '-1004444444444',
    ]) {
      expect(
        _chip(id),
        findsOneWidget,
        reason: '$id should be a verified chip',
      );
    }
    // The task's identity is readable in the detail view.
    expect(find.text('#abcdefgh'), findsOneWidget);
    // And the route is previewed before anything is saved.
    expect(
      tester
          .widget<Text>(
            find.byKey(const ValueKey<String>('rule_route_preview')),
          )
          .data,
      '-1001111111111, -1002222222222 → -1003333333333, -1004444444444',
    );
  });

  testWidgets('typing two ids per side sends both lists on save', (
    tester,
  ) async {
    final log = <_Sent>[];
    await _pumpEditor(tester, _state(log), rule: _twoByTwo());

    await tester.enterText(_field('rule_sources_field'), 'A, B');
    await tester.enterText(_field('rule_targets_field'), 'X, Y');
    await tester.pump();

    expect(find.byType(InputChip), findsNWidgets(4));
    await _tapSave(tester);

    final sent = _writes(log).last;
    expect(sent.method, 'PUT');
    final body = sent.body! as Map<String, dynamic>;
    expect(body['source_chat_ids'], const ['A', 'B']);
    expect(body['target_chat_ids'], const ['X', 'Y']);
    // The legacy singulars still carry the primary endpoints.
    expect(body['source_chat_id'], 'A');
    expect(body['target_chat_id'], 'X');
  });

  testWidgets('a duplicate inside a list is named and blocks the save', (
    tester,
  ) async {
    final log = <_Sent>[];
    await _pumpEditor(tester, _state(log), rule: _twoByTwo());

    await tester.enterText(
      _field('rule_sources_field'),
      '-1001111111111, -1002222222222, -1001111111111',
    );
    await tester.pump();

    expect(
      find.textContaining('-1001111111111 is listed more than once'),
      findsOneWidget,
    );

    await _tapSave(tester);

    expect(
      find.textContaining('-1001111111111 is listed more than once'),
      findsOneWidget,
    );
    expect(_writes(log), isEmpty, reason: 'a duplicate must never be sent');
  });

  testWidgets('a chat used on both sides is named and blocks the save', (
    tester,
  ) async {
    final log = <_Sent>[];
    await _pumpEditor(tester, _state(log), rule: _twoByTwo());

    await tester.enterText(
      _field('rule_sources_field'),
      '-1001111111111, @news',
    );
    await tester.enterText(
      _field('rule_targets_field'),
      '-1003333333333, https://t.me/news',
    );
    await tester.pump();

    // Same chat, two spellings: the overlap is caught and the typed entry named.
    expect(
      find.textContaining('@news is both a source and a target'),
      findsOneWidget,
    );

    await _tapSave(tester);

    expect(
      find.textContaining('@news is both a source and a target'),
      findsOneWidget,
    );
    expect(_writes(log), isEmpty, reason: 'a self-loop must never be sent');
  });

  testWidgets('a legacy single-source rule still renders and edits', (
    tester,
  ) async {
    final log = <_Sent>[];
    final legacy = ForwardRule.fromJson(const {
      'id': 'r-legacy-0001-0002',
      'source_chat_id': '-1001111111111',
      'target_chat_id': '-1003333333333',
      'session_id': 's-1',
      'is_enabled': true,
    });

    await _pumpEditor(tester, _state(log), rule: legacy);

    expect(find.byType(InputChip), findsNWidgets(2));
    expect(_chip('-1001111111111'), findsOneWidget);
    expect(_chip('-1003333333333'), findsOneWidget);
    expect(_fieldText(tester, 'rule_sources_field'), '-1001111111111');
    expect(_fieldText(tester, 'rule_targets_field'), '-1003333333333');

    // ...and it can be widened to a list from the same screen.
    await tester.enterText(
      _field('rule_sources_field'),
      '-1001111111111, -1002222222222',
    );
    await tester.pump();
    await _tapSave(tester);

    final body = _writes(log).last.body! as Map<String, dynamic>;
    expect(body['source_chat_ids'], const ['-1001111111111', '-1002222222222']);
    expect(body['source_chat_id'], '-1001111111111');
  });

  testWidgets('an entry can be removed, and tapped to fix it in place', (
    tester,
  ) async {
    final log = <_Sent>[];
    await _pumpEditor(tester, _state(log), rule: _twoByTwo());

    // Tapping a rejected/accepted chip selects that entry in its field.
    await tester.ensureVisible(_chip('-1001111111111'));
    await tester.tap(_chip('-1001111111111'));
    await tester.pump();
    final ctrl =
        tester.widget<TextFormField>(_field('rule_sources_field')).controller!;
    expect(ctrl.selection.textInside(ctrl.text), '-1001111111111');

    // Removing a chip drops exactly that entry, keeping the rest in order.
    await tester.ensureVisible(_chip('-1002222222222'));
    await tester.tap(
      find.descendant(
        of: _chip('-1002222222222'),
        matching: find.byIcon(Icons.close),
      ),
    );
    await tester.pump();

    expect(_chip('-1002222222222'), findsNothing);
    expect(_chip('-1001111111111'), findsOneWidget);
    expect(_fieldText(tester, 'rule_sources_field'), '-1001111111111');
  });
}
