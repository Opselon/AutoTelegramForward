/// `ForwardRule` state parsing: `is_active` and `is_paused` are two
/// independent server flags, and the three real states — running, paused,
/// stopped — must all parse distinctly.
///
/// The old parse read `is_enabled`, a key the Go projection never sends, and
/// fell back to the inverted pause flag. A rule stopped with
/// `POST /rules/{id}/stop` (`is_active=false`, `is_paused=false`) therefore
/// rendered as Active — a lie this test pins shut.
library;

import 'package:flutter_test/flutter_test.dart';

import 'package:autoforward_mobile/core/models.dart';

ForwardRule _rule(Map<String, dynamic> flags) => ForwardRule.fromJson({
  'id': 'rule-1',
  'source_chat_id': '-1001111111111',
  'target_chat_id': '-1002222222222',
  ...flags,
});

void main() {
  test('a running rule: active, not paused', () {
    final rule = _rule({'is_active': true, 'is_paused': false});
    expect(rule.isEnabled, isTrue);
    expect(rule.isPaused, isFalse);
    expect(rule.isRunning, isTrue);
  });

  test('a paused rule is still active but held', () {
    final rule = _rule({'is_active': true, 'is_paused': true});
    expect(
      rule.isEnabled,
      isTrue,
      reason: 'pause must not flip the master switch',
    );
    expect(rule.isPaused, isTrue);
    expect(rule.isRunning, isFalse);
  });

  test('a stopped rule (is_active=false) must not parse as enabled', () {
    final rule = _rule({'is_active': false, 'is_paused': false});
    expect(
      rule.isEnabled,
      isFalse,
      reason: 'the regression: stopped read as active',
    );
    expect(rule.isPaused, isFalse);
    expect(rule.isRunning, isFalse);
  });

  test('the legacy is_enabled key still works for servers that send it', () {
    final rule = _rule({'is_enabled': false});
    expect(rule.isEnabled, isFalse);
  });

  test('absent flags default to a running rule', () {
    final rule = _rule({});
    expect(rule.isEnabled, isTrue);
    expect(rule.isPaused, isFalse);
    expect(rule.isRunning, isTrue);
  });

  test('toUpdateJson sends is_active (the field the proto binds)', () {
    final rule = _rule({'is_active': true});
    final body = rule.toUpdateJson();
    expect(body['is_active'], isTrue);
    expect(body['expected_version'], rule.version);
  });
}
