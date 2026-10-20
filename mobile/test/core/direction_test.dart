/// Direction-aware icons must actually mirror.
///
/// The contract asks for mirrored directional icons in RTL. A chevron that
/// still points right in Persian is the kind of bug that survives review
/// because the reviewer reads LTR. This test resolves the helper under both
/// directions and pins the result.
library;

import 'package:autoforward_mobile/core/layout.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

Future<IconData> _resolve(
  WidgetTester tester,
  TextDirection direction,
  IconData Function(BuildContext) pick,
) async {
  late IconData resolved;
  await tester.pumpWidget(
    Directionality(
      textDirection: direction,
      child: Builder(
        builder: (context) {
          resolved = pick(context);
          return const SizedBox();
        },
      ),
    ),
  );
  return resolved;
}

void main() {
  testWidgets('disclosure and arrow icons point the other way in RTL', (
    tester,
  ) async {
    expect(
      await _resolve(tester, TextDirection.ltr, AppIcons.forward),
      Icons.chevron_right,
    );
    expect(
      await _resolve(tester, TextDirection.rtl, AppIcons.forward),
      Icons.chevron_left,
    );
    expect(
      await _resolve(tester, TextDirection.ltr, AppIcons.forwardRounded),
      Icons.chevron_right_rounded,
    );
    expect(
      await _resolve(tester, TextDirection.rtl, AppIcons.forwardRounded),
      Icons.chevron_left_rounded,
    );
    expect(
      await _resolve(tester, TextDirection.ltr, AppIcons.arrow),
      Icons.arrow_forward_rounded,
    );
    expect(
      await _resolve(tester, TextDirection.rtl, AppIcons.arrow),
      Icons.arrow_back_rounded,
    );
  });
}
