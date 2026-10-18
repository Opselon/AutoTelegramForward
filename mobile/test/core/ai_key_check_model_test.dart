/// The two vocabulary bridges between the server's codes and this app's
/// catalogs: a probe verdict, and a save refusal.
library;

import 'package:flutter_test/flutter_test.dart';

import 'package:autoforward_mobile/core/models.dart';

void main() {
  test('a healthy probe parses with its latency and no reason', () {
    final result = KeyCheckResult.fromJson(const {
      'success': true,
      'healthy': true,
      'latency_ms': 214.6,
      'detail': '',
      'error_code': '',
    });
    expect(result.success, isTrue);
    expect(result.healthy, isTrue);
    expect(result.latencyMs, closeTo(214.6, 0.001));
    expect(result.reasonKey, isEmpty);
  });

  test('a refused probe keeps its code and the provider wording', () {
    final result = KeyCheckResult.fromJson(const {
      'success': false,
      'healthy': false,
      'latency_ms': '90',
      'detail': 'Incorrect API key provided: [REDACTED]',
      'error_code': 'invalid_key',
    });
    expect(result.healthy, isFalse);
    expect(result.reasonKey, 'ai_check_invalid_key');
    expect(result.detail, contains('Incorrect API key'));
  });

  test('a blocked endpoint is not confused with a rejected key', () {
    final result = KeyCheckResult.fromJson(const {
      'success': false,
      'healthy': false,
      'error_code': 'endpoint_blocked',
    });
    expect(result.reasonKey, 'ai_check_endpoint_blocked');
    expect(result.reasonKey, isNot('ai_check_invalid_key'));
  });

  test('an unknown code yields nothing rather than a raw identifier', () {
    final result = KeyCheckResult.fromJson(const {
      'success': false,
      'healthy': false,
      'error_code': 'something_the_server_added_later',
    });
    expect(result.reasonKey, isEmpty);
  });

  test('a numeric string latency does not throw', () {
    final result = KeyCheckResult.fromJson(const {'latency_ms': '12.5'});
    expect(result.latencyMs, closeTo(12.5, 0.001));
  });

  test('a save refusal that is a known code maps to a catalog key', () {
    expect(aiSaveFailureKey('endpoint_blocked'), 'ai_check_endpoint_blocked');
    expect(aiSaveFailureKey('endpoint_invalid'), 'ai_check_endpoint_invalid');
    expect(
      aiSaveFailureKey('  endpoint_blocked  '),
      'ai_check_endpoint_blocked',
    );
  });

  test('a save failure that is prose is left alone', () {
    expect(aiSaveFailureKey('AI config not found'), isEmpty);
    expect(aiSaveFailureKey(''), isEmpty);
  });

  test('a key the user typed is sent once and never read back', () {
    const config = AiConfig(
      id: '',
      provider: 'openai',
      model: 'gpt-4o',
      apiKey: 'tenant-key-value',
      isEnabled: true,
    );
    expect(config.toJson()['api_key'], 'tenant-key-value');
    // The server never returns a key, so an edit must not resend one.
    const blank = AiConfig(id: 'cfg-1', provider: 'openai', isEnabled: true);
    expect(blank.toJson().containsKey('api_key'), isFalse);
  });
}
