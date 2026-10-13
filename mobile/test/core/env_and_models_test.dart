import 'package:flutter_test/flutter_test.dart';

import 'package:autoforward_mobile/core/api_client.dart';
import 'package:autoforward_mobile/core/env.dart';
import 'package:autoforward_mobile/core/models.dart';

void main() {
  group('ServerUrl validation', () {
    test('accepts valid https URL', () {
      final url = ServerUrl.tryParse('https://atf.example.com');
      expect(url, isNotNull);
      expect(url!.normalized, 'https://atf.example.com/');
      expect(url.join('api/rules'), 'https://atf.example.com/api/rules');
    });

    test('accepts local loopback http for development', () {
      final url = ServerUrl.tryParse('http://10.0.2.2:8080');
      expect(url, isNotNull);
      expect(url!.normalized, 'http://10.0.2.2:8080/');

      final localhost = ServerUrl.tryParse('http://localhost:8080');
      expect(localhost, isNotNull);
    });

    test('rejects non-https for remote hosts', () {
      final url = ServerUrl.tryParse('http://remote.server.com');
      expect(url, isNull);
    });

    test('rejects malformed URLs', () {
      expect(ServerUrl.tryParse(''), isNull);
      expect(ServerUrl.tryParse('not-a-url'), isNull);
      expect(ServerUrl.tryParse('ftp://example.com'), isNull);
      expect(ServerUrl.tryParse('https://example.com?query=1'), isNull);
    });
  });

  group('Models serialization', () {
    test('ForwardRule fromJson and toJson', () {
      final json = {
        'id': 'r-1',
        'session_id': 's-1',
        'source_chat_id': '-1001',
        'source_chat_title': 'Source',
        'target_chat_id': '-1002',
        'target_chat_title': 'Target',
        'intermediate_channel_id': '-1003',
        'use_vip_routing': true,
        'filters': ['apple', 'banana'],
        'filter_mode': 'all',
        'media_mode': 'all',
        'delay_seconds': 5,
        'header_template': 'Header',
        'footer_template': 'Footer',
        'version': 3,
        'is_enabled': true,
        'owner_user_id': 42,
      };

      final rule = ForwardRule.fromJson(json);
      expect(rule.id, 'r-1');
      expect(rule.sourceChatId, '-1001');
      expect(rule.filters, ['apple', 'banana']);
      expect(rule.useVipRouting, isTrue);
      expect(rule.version, 3);
      expect(rule.ownerUserId, 42);

      final updateJson = rule.toUpdateJson();
      expect(updateJson['expected_version'], 3);
      expect(updateJson['is_enabled'], isTrue);
    });

    test('TelegramSession fromJson', () {
      final json = {
        'id': 'sess-1',
        'is_active': true,
        'phone_number': '+989123456789',
        'user_id': '12345678',
        'username': 'tguser',
        'first_name': 'Ali',
        'owner_user_id': 42,
      };

      final session = TelegramSession.fromJson(json);
      expect(session.id, 'sess-1');
      expect(session.isActive, isTrue);
      expect(session.displayName, 'Ali');
    });

    test('SystemStats fromJson handles nested rules and counts', () {
      final json = {
        'rules': {'total': 10, 'active': 7},
        'total_sessions': 3,
        'active_sessions': 2,
        'core_running': true,
        'version': '1.4.1',
        'uptime_seconds': 1200,
        'delivered_total': 500,
        'failed_total': 2,
      };

      final stats = SystemStats.fromJson(json);
      expect(stats.totalRules, 10);
      expect(stats.activeRules, 7);
      expect(stats.coreRunning, isTrue);
      expect(stats.deliveredTotal, 500);
    });

    test('LoginChallenge parsing', () {
      final json = {
        'challenge_id': 'ch-abc',
        'state': 'awaiting_code',
        'message': 'code_sent',
        'retry_after_seconds': 60,
      };

      final ch = LoginChallenge.fromJson(json);
      expect(ch.id, 'ch-abc');
      expect(ch.state, 'awaiting_code');
      expect(ch.retryAfterSeconds, 60);
    });
  });
}
