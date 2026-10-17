/// Numeric reads must survive a server that stringifies numbers.
///
/// Every case here is a real shape of the live API: `topic_id: ""` (the storage
/// default), `user_id: "5094837833"` (a 64-bit Telegram id sent as a string so a
/// JavaScript client cannot lose precision), and a `version` like `"1.2.2"`.
/// The shipped client read them with `as num?`, which raised
/// `type 'String' is not a subtype of type 'num?' in type cast` and blanked the
/// screen that was parsing.
library;

import 'package:autoforward_mobile/core/json_cast.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('jsonNum', () {
    test('passes numbers through, including a bool-shaped zero', () {
      expect(jsonNum(0), 0);
      expect(jsonNum(18), 18);
      expect(jsonNum(0.7), 0.7);
      expect(jsonNum(-1004400615270), -1004400615270);
    });

    test('reads a numeric string, trimmed', () {
      expect(jsonNum('42'), 42);
      expect(jsonNum(' 42 '), 42);
      expect(jsonNum('-1004400615270'), -1004400615270);
      expect(jsonNum('0.7'), 0.7);
      expect(jsonNum('5094837833'), 5094837833);
    });

    test('an empty or blank string is absent, not zero', () {
      expect(jsonNum(''), isNull);
      expect(jsonNum('   '), isNull);
    });

    test('a non-numeric string is absent, not a guess', () {
      expect(jsonNum('1.2.2'), isNull);
      expect(jsonNum('n/a'), isNull);
      expect(jsonNum('NaN'), isNull);
      expect(jsonNum('12abc'), isNull);
    });

    test('a value of another type is absent', () {
      expect(jsonNum(null), isNull);
      expect(jsonNum(true), isNull);
      expect(jsonNum(<String>['1']), isNull);
      expect(jsonNum(<String, dynamic>{'v': 1}), isNull);
    });
  });

  group('jsonInt', () {
    test('truncates numbers and parses numeric strings', () {
      expect(jsonInt(5), 5);
      expect(jsonInt(5.9), 5);
      expect(jsonInt('42'), 42);
      expect(jsonInt('5.9'), 5);
    });

    test('uses the fallback for an absent or unusable value', () {
      expect(jsonInt(null), 0);
      expect(jsonInt(''), 0);
      expect(jsonInt('abc', 600), 600);
      expect(jsonInt('1.2.2', 3), 3);
    });

    test('never throws on any payload value', () {
      for (final value in <Object?>[
        null,
        '',
        '   ',
        '1.2.2',
        true,
        false,
        <String>['1'],
        <String, dynamic>{'a': 1},
      ]) {
        expect(() => jsonInt(value), returnsNormally, reason: '$value');
      }
    });
  });

  group('jsonIntOrNull', () {
    test('keeps "not reported" distinct from an explicit zero', () {
      expect(jsonIntOrNull(''), isNull);
      expect(jsonIntOrNull(null), isNull);
      expect(jsonIntOrNull('0'), 0);
      expect(jsonIntOrNull(0), 0);
    });
  });

  group('jsonDouble', () {
    test('parses the AI creativity the server sends either way', () {
      expect(jsonDouble(0.7), 0.7);
      expect(jsonDouble('0.7'), 0.7);
      expect(jsonDouble('0.8500000238418579'), closeTo(0.85, 1e-6));
    });

    test('falls back when absent', () {
      expect(jsonDouble(''), 0);
      expect(jsonDouble('', 0.7), 0.7);
      expect(jsonDouble('abc', 0.7), 0.7);
    });
  });

  group('jsonIntMap', () {
    test('reads a counts map whose values may be strings', () {
      expect(jsonIntMap({'error': 3, 'warning': '2', 'info': ''}), {
        'error': 3,
        'warning': 2,
        'info': 0,
      });
    });

    test('a non-map is an empty map, not a crash', () {
      expect(jsonIntMap(null), isEmpty);
      expect(jsonIntMap('3'), isEmpty);
      expect(jsonIntMap(<int>[1, 2]), isEmpty);
    });
  });
}
