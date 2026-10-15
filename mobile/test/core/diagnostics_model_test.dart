/// Parsing contract for `GET /api/diagnostics`.
///
/// The server omits or nests fields (counts/scanned are sub-objects, findings
/// arrive as loose maps); a lenient parse is what keeps an older or partially
/// deployed server from crashing the dashboard.
library;

import 'package:autoforward_mobile/core/models.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('DiagnosticsReport.fromJson', () {
    test('parses a full payload', () {
      final report = DiagnosticsReport.fromJson({
        'status': 'critical',
        'score': 0,
        'counts': {'error': 2, 'warning': 1, 'info': 3},
        'scanned': {'rules': 9, 'ai_configs': 4},
        'services': [
          {
            'name': 'core',
            'status': 'up',
            'detail': 'gRPC ok',
            'latency_ms': 12,
          },
        ],
        'findings': [
          {
            'id': 'ai.endpoint_unreachable:cfg1',
            'severity': 'error',
            'scope': 'ai',
            'title': 'AI endpoint unreachable',
            'detail': 'connection refused',
            'remediation': 'Point base_url at a reachable gateway.',
          },
        ],
      });

      expect(report.status, 'critical');
      expect(report.isHealthy, isFalse);
      expect(report.errorCount, 2);
      expect(report.warningCount, 1);
      expect(report.infoCount, 3);
      expect(report.rulesScanned, 9);
      expect(report.aiConfigsScanned, 4);
      expect(report.services, hasLength(1));
      expect(report.services.first.name, 'core');
      expect(report.findings.single.isError, isTrue);
      expect(report.findings.single.scope, 'ai');
      expect(report.findings.single.remediation, contains('base_url'));
    });

    test('a healthy report with no findings parses to an empty list', () {
      final report = DiagnosticsReport.fromJson({
        'status': 'healthy',
        'score': 100,
        'counts': {'error': 0, 'warning': 0, 'info': 0},
        'scanned': {'rules': 0, 'ai_configs': 0},
        'services': [],
        'findings': [],
      });

      expect(report.isHealthy, isTrue);
      expect(report.findings, isEmpty);
      expect(report.score, 100);
    });

    test('missing optional sections degrade instead of throwing', () {
      // A minimal/older payload: only the grade is guaranteed.
      final report = DiagnosticsReport.fromJson({'status': 'warning'});

      expect(report.status, 'warning');
      expect(report.score, 0);
      expect(report.findings, isEmpty);
      expect(report.services, isEmpty);
      expect(report.errorCount, 0);
    });

    test('severity is normalised and null-safe', () {
      final f = DiagnosticFinding.fromJson({'severity': 'WARNING'});
      expect(f.severity, 'warning');
      expect(f.isWarning, isTrue);
      expect(f.isError, isFalse);
      expect(f.title, isEmpty);

      final blank = DiagnosticFinding.fromJson(const {});
      expect(blank.severity, 'info');
      expect(blank.isError, isFalse);
      expect(blank.isWarning, isFalse);
    });
  });
}
