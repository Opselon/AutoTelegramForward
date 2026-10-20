/// Toast primitive: transient success/failure feedback.
///
/// One place decides how a result is announced, so a rule save, a copy to the
/// clipboard and a failed export all look and read the same. The message is a
/// catalog string supplied by the caller — this widget never invents copy.
///
/// Announcement: the snack bar content is wrapped in a live region, which is the
/// supported way to have TalkBack/VoiceOver read it when it appears (the older
/// `SemanticsService.announce` call is deprecated in this Flutter version).
library;

import 'package:flutter/material.dart';

import '../core/design_tokens.dart';

enum ToastTone { success, danger, info }

class AppToast {
  const AppToast._();

  static void show(
    BuildContext context,
    String message, {
    ToastTone tone = ToastTone.info,
  }) {
    final palette = AppColors.of(context);
    final messenger = ScaffoldMessenger.maybeOf(context);
    if (messenger == null) return;

    final color = switch (tone) {
      ToastTone.success => palette.success,
      ToastTone.danger => palette.danger,
      ToastTone.info => palette.info,
    };
    final icon = switch (tone) {
      ToastTone.success => Icons.check_circle_outline,
      ToastTone.danger => Icons.error_outline,
      ToastTone.info => Icons.info_outline,
    };

    messenger
      ..hideCurrentSnackBar()
      ..showSnackBar(
        SnackBar(
          duration: const Duration(seconds: 3),
          content: Semantics(
            liveRegion: true,
            label: message,
            child: Row(
              children: [
                Icon(icon, size: 18, color: color),
                const SizedBox(width: AppSpacing.sm),
                Expanded(
                  child: Text(
                    message,
                    maxLines: 3,
                    style: const TextStyle(
                      color: Colors.white,
                      fontSize: AppType.md,
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      );
  }

  static void success(BuildContext context, String message) =>
      show(context, message, tone: ToastTone.success);

  static void failure(BuildContext context, String message) =>
      show(context, message, tone: ToastTone.danger);
}
