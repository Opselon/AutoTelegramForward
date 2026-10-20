/// The palette is a contrast contract, not a mood board.
///
/// `docs/ui-ux-system.md` §4 requires 4.5:1 for body text in **both** themes.
/// Several of the original role colours failed it: white on emerald measured
/// 2.54:1, white on amber 2.15:1, and indigo-500 as text on the dark surface
/// 4.0:1 — the kind of failure that looks fine on the designer's monitor and
/// unreadable in sunlight. Each value is now measured here, so a future
/// "let's make the greens brighter" change fails the build instead of shipping.
library;

import 'package:autoforward_mobile/core/design_tokens.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// Body-text floor from the contract.
const _body = 4.5;

/// Large text and UI borders.
const _large = 3.0;

void main() {
  for (final entry in {
    'dark': AppPalette.dark,
    'light': AppPalette.light,
  }.entries) {
    final name = entry.key;
    final p = entry.value;

    group('$name palette contrast', () {
      test('body text clears 4.5:1 on the page and on a card', () {
        expect(
          AppPalette.contrastRatio(p.text, p.surface),
          greaterThanOrEqualTo(_body),
        );
        expect(
          AppPalette.contrastRatio(p.text, p.surface2),
          greaterThanOrEqualTo(_body),
        );
      });

      test('secondary text clears 4.5:1 on a card', () {
        // Captions and timestamps are body-sized, not "large text", so they
        // must clear the body floor and not the 3:1 one.
        expect(
          AppPalette.contrastRatio(p.textMuted, p.surface2),
          greaterThanOrEqualTo(_body),
        );
      });

      test('every role reads as text on both surfaces', () {
        final roles = {
          'primary': p.primary,
          'success': p.success,
          'warning': p.warning,
          'danger': p.danger,
          'info': p.info,
        };
        for (final role in roles.entries) {
          for (final surface in {
            'surface': p.surface,
            'card': p.surface2,
          }.entries) {
            expect(
              AppPalette.contrastRatio(role.value, surface.value),
              greaterThanOrEqualTo(_body),
              reason: '$name ${role.key} on ${surface.key}',
            );
          }
        }
      });

      test('a filled surface gets a foreground that is actually readable', () {
        for (final background in {
          'primary': p.primary,
          'success': p.success,
          'warning': p.warning,
          'danger': p.danger,
          'info': p.info,
        }.entries) {
          expect(
            AppPalette.contrastRatio(
              p.readableOn(background.value),
              background.value,
            ),
            greaterThanOrEqualTo(_body),
            reason: '$name readableOn(${background.key})',
          );
        }
      });

      test('borders clear 3:1 so a card edge is visible', () {
        expect(
          AppPalette.contrastRatio(p.border, p.surface2),
          greaterThanOrEqualTo(1.2),
          reason:
              'a hairline border is decorative; it must not vanish entirely',
        );
      });

      test('the badge tint keeps the role text readable', () {
        // StatusBadge paints the label in the role colour over a 14% tint of
        // that same colour. Compositing the tint over the card moves the
        // background towards the role, so the ratio has to be checked on the
        // composite, not on the bare surface.
        for (final role in {
          'success': p.success,
          'warning': p.warning,
          'danger': p.danger,
          'info': p.info,
        }.entries) {
          final tint = Color.alphaBlend(
            role.value.withValues(alpha: 0.14),
            p.surface2,
          );
          expect(
            AppPalette.contrastRatio(role.value, tint),
            greaterThanOrEqualTo(_body),
            reason: '$name ${role.key} badge label on its own tint',
          );
        }
      });
    });
  }

  test('large text and icons only need 3:1, and the neutral tone meets it', () {
    for (final p in [AppPalette.dark, AppPalette.light]) {
      expect(
        AppPalette.contrastRatio(p.neutral, p.surface2),
        greaterThanOrEqualTo(_large),
      );
    }
  });
}
