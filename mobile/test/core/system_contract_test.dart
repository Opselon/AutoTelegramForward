/// Contract tests: the mobile models against payloads captured from the real
/// API, shared with the other clients.
///
/// `contracts/fixtures/*.json` are verbatim responses from `atf-api` (captured
/// with an authenticated request), consumed here, by the Go handler tests and by
/// the web client. Parsing the same bytes everywhere is what keeps the two ends
/// from drifting: a renamed JSON field fails this test on the client side and
/// the Go test on the server side, instead of silently rendering blanks.
///
/// When the API payload changes, re-capture the fixture
/// (`scripts/capture_contract_fixtures.py`) and update it in the same change.
library;

import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

import 'package:autoforward_mobile/core/models.dart';

Map<String, dynamic> _fixture(String name) {
  final file = File('../contracts/fixtures/$name.json');
  expect(file.existsSync(), isTrue, reason: 'missing fixture ${file.path}');
  return jsonDecode(file.readAsStringSync()) as Map<String, dynamic>;
}

void main() {
  group('SetupReport (GET /api/system/setup)', () {
    late SetupReport report;

    setUp(() {
      report = SetupReport.fromJson(_fixture('system_setup'));
    });

    test('parses the live payload without loss', () {
      expect(report.ready, isTrue);
      expect(report.deployment, isNotEmpty);
      expect(report.apiVersion, isNotEmpty);
      expect(report.features, isNotEmpty);
      expect(report.checks, isNotEmpty);
      expect(report.services, isNotEmpty);
      expect(report.missingRequired, isEmpty);
      expect(report.time, isNotEmpty);
    });

    test('service probes carry name, state, detail and latency', () {
      for (final service in report.services) {
        expect(service.name, isNotEmpty);
        expect(service.detail, isNotEmpty);
        expect(service.latencyMs, greaterThanOrEqualTo(0));
      }
      expect(report.onlineServices, report.services.length);
    });

    test('the contract is booleans only - no secret value can round-trip', () {
      final raw = _fixture('system_setup')['checks'] as List;
      for (final entry in raw) {
        final check = entry as Map<String, dynamic>;
        // Exactly these keys: a `value` key would mean the API started echoing
        // configuration back to clients.
        expect(check.keys.toSet(), {
          'key',
          'level',
          'set',
          'reason',
          'hint',
        }, reason: 'unexpected field in a contract check: $check');
        expect(check['set'], isA<bool>());
      }
      for (final check in report.checks) {
        expect(check.level, isIn(['required', 'recommended', 'optional']));
      }
    });

    test('required checks are marked as such', () {
      final required = report.checks.where((c) => c.isRequired).toList();
      expect(required, isNotEmpty);
      expect(
        required.every((c) => c.set),
        isTrue,
        reason: 'a ready server must have its required keys set',
      );
    });

    test('a minimal payload degrades instead of throwing', () {
      final minimal = SetupReport.fromJson({'ready': true});
      expect(minimal.ready, isTrue);
      expect(minimal.features, isEmpty);
      expect(minimal.checks, isEmpty);
      expect(minimal.services, isEmpty);
      expect(minimal.onlineServices, 0);
      expect(minimal.hasContractGaps, isFalse);
    });

    test('a payload with contract gaps is flagged', () {
      final gap = SetupReport.fromJson({
        'ready': false,
        'missing_required': ['ATF_CORE_GRPC'],
        'checks': [
          {
            'key': 'ATF_CORE_GRPC',
            'level': 'required',
            'set': false,
            'reason': 'core gRPC address',
            'hint': 'set ATF_CORE_GRPC',
          },
        ],
      });
      expect(gap.ready, isFalse);
      expect(gap.hasContractGaps, isTrue);
      expect(gap.checks.single.set, isFalse);
    });
  });

  group('ServiceHealth (GET /api/v1/health/services)', () {
    test('parses the live payload including the list key', () {
      final payload = _fixture('health_services');
      final services = (payload['services'] as List)
          .whereType<Map>()
          .map((e) => ServiceHealth.fromJson(e.cast<String, dynamic>()))
          .toList();
      expect(services, isNotEmpty);
      expect(payload['status'], isIn(['healthy', 'degraded']));
      expect(services.map((s) => s.name), contains('core'));
    });
  });

  group('EnvContract (GET /api/system/env)', () {
    test('reports set/missing per key and never a value', () {
      final payload = _fixture('system_env');
      expect(payload['ok'], isA<bool>());
      expect(payload['deployment'], isA<String>());
      final checks = (payload['checks'] as List)
          .whereType<Map>()
          .map((e) => EnvCheck.fromJson(e.cast<String, dynamic>()))
          .toList();
      expect(checks, isNotEmpty);
      expect(checks.any((c) => c.set), isTrue);
      for (final check in checks) {
        expect(check.key, isNotEmpty);
        // The whole contract is booleans: no value field exists to leak.
        expect(check.runtimeType, EnvCheck);
      }
    });
  });
}
