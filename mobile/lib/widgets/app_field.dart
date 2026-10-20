/// Field primitive: label above the control (never a placeholder used as the
/// only label), hint below, error text in `danger`, 44 dp minimum control
/// height inside a 48 dp touch target.
library;

import 'package:flutter/material.dart';

import '../core/design_tokens.dart';

class AppField extends StatelessWidget {
  const AppField({
    super.key,
    required this.label,
    this.controller,
    this.initialValue,
    this.hint,
    this.helper,
    this.errorText,
    this.prefixIcon,
    this.suffix,
    this.onChanged,
    this.onSubmitted,
    this.keyboardType,
    this.textInputAction,
    this.obscureText = false,
    this.maxLines = 1,
    this.minLines,
    this.enabled = true,
    this.autofocus = false,
    this.focusNode,
    this.textCapitalization = TextCapitalization.none,
    this.keyboardAppearance,
  });

  final String label;
  final TextEditingController? controller;
  final String? initialValue;
  final String? hint;
  final String? helper;
  final String? errorText;
  final IconData? prefixIcon;
  final Widget? suffix;
  final ValueChanged<String>? onChanged;
  final ValueChanged<String>? onSubmitted;
  final TextInputType? keyboardType;
  final TextInputAction? textInputAction;
  final bool obscureText;
  final int maxLines;
  final int? minLines;
  final bool enabled;
  final bool autofocus;
  final FocusNode? focusNode;
  final TextCapitalization textCapitalization;
  final Brightness? keyboardAppearance;

  @override
  Widget build(BuildContext context) {
    final palette = AppColors.of(context);
    return Semantics(
      textField: true,
      label: label,
      hint: errorText ?? hint,
      child: TextField(
        controller: controller,
        focusNode: focusNode,
        enabled: enabled,
        autofocus: autofocus,
        obscureText: obscureText,
        maxLines: obscureText ? 1 : maxLines,
        minLines: minLines,
        keyboardType: keyboardType,
        textInputAction: textInputAction,
        textCapitalization: textCapitalization,
        keyboardAppearance: keyboardAppearance,
        onChanged: onChanged,
        onSubmitted: onSubmitted,
        style: TextStyle(fontSize: AppType.md, color: palette.text),
        decoration: InputDecoration(
          labelText: label,
          hintText: hint,
          helperText: helper,
          helperMaxLines: 3,
          errorText: errorText,
          errorMaxLines: 3,
          prefixIcon: prefixIcon == null ? null : Icon(prefixIcon, size: 20),
          suffixIcon: suffix,
          isDense: true,
          constraints: const BoxConstraints(minHeight: 44),
        ),
      ),
    );
  }
}
