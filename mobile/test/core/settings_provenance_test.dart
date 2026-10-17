/// Parsing what the settings endpoint returns once it can carry provenance.
///
/// The failure this guards against is subtle: if the client edits the *stored*
/// map instead of the effective one, an inherited value the user never touched
/// looks like a change and gets posted back — pinning it, and quietly ending the
/// inheritance. `baseline` is what the sheet edits, so it is asserted here.
library;

import 'package:flutter_test/flutter_test.dart';

import 'package:autoforward_mobile/core/models.dart';

void main() {
  test('a full response keeps all three views apart', () {
    final view = RuleSettingsView.fromJson(<String, dynamic>{
      'settings': <String, dynamic>{'remove_emojis': false, 'signature': ''},
      'effective': <String, dynamic>{'remove_emojis': true, 'signature': 'hi'},
      'sources': <String, dynamic>{
        'remove_emojis': <String, dynamic>{
          'value': true,
          'source': 'bot',
          'default': false,
          'inheritable': true,
          'bot_default': true,
          'bot_label': '@alice_bot',
        },
        'signature': <String, dynamic>{
          'value': 'hi',
          'source': 'task',
          'default': '',
          'inheritable': true,
          'pinned': true,
        },
      },
    });

    expect(view.baseline, <String, dynamic>{
      'remove_emojis': true,
      'signature': 'hi',
    });
    final inherited = view.provenanceFor('remove_emojis')!;
    expect(inherited.isBot, isTrue);
    expect(inherited.isTask, isFalse);
    expect(inherited.botLabel, '@alice_bot');
    final owned = view.provenanceFor('signature')!;
    expect(owned.isTask, isTrue);
    expect(owned.pinned, isTrue);
  });

  test(
    'an older response without provenance still yields an editable baseline',
    () {
      final view = RuleSettingsView.fromJson(<String, dynamic>{
        'settings': <String, dynamic>{'remove_emojis': false},
      });

      expect(view.baseline, <String, dynamic>{'remove_emojis': false});
      expect(view.sources, isEmpty);
      expect(view.provenanceFor('remove_emojis'), isNull);
    },
  );

  test('a bare settings map is read as the settings map', () {
    final view = RuleSettingsView.fromJson(<String, dynamic>{
      'remove_emojis': true,
    });

    expect(view.baseline, <String, dynamic>{'remove_emojis': true});
  });

  test('an unknown source is not mistaken for the task or the bot', () {
    final info = SettingProvenance.fromJson(<String, dynamic>{
      'value': 3,
      'source': 'something_new',
    });

    expect(info.isTask, isFalse);
    expect(info.isBot, isFalse);
    expect(info.isDefault, isFalse);
    expect(info.value, 3);
  });

  test('a missing source string does not throw', () {
    final info = SettingProvenance.fromJson(<String, dynamic>{'value': null});

    expect(info.source, '');
    expect(info.botLabel, '');
    expect(info.pinned, isFalse);
  });

  test('the schema reports whether a bot may supply a setting', () {
    final inheritable = RuleSettingSpec.fromJson(<String, dynamic>{
      'key': 'remove_emojis',
      'kind': 'bool',
      'group': 'cleanup',
      'label': 'Remove emoji',
      'inheritable': true,
    });
    final route = RuleSettingSpec.fromJson(<String, dynamic>{
      'key': 'delivery_transport',
      'kind': 'str',
      'group': 'route',
      'label': 'Delivery route',
      'inheritable': false,
    });
    // An older server sends no flag at all; behaviour settings are inheritable.
    final legacy = RuleSettingSpec.fromJson(<String, dynamic>{
      'key': 'remove_links',
      'kind': 'bool',
      'group': 'cleanup',
      'label': 'Remove links',
    });

    expect(inheritable.inheritable, isTrue);
    expect(route.inheritable, isFalse);
    expect(legacy.inheritable, isTrue);
  });
}
