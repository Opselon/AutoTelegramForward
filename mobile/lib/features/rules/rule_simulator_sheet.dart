/// The dry run: what *would* happen to a sample message.
///
/// This answers the question a settings screen cannot — "I turned on remove
/// emojis and strip links; what will come out the other side?" — by asking the
/// server to run the real engine against a real sample. Three properties:
///   * it forwards nothing: the call carries one owned rule id and evaluates;
///   * it reports the server's verdict verbatim, including *why* a message
///     would be skipped, so a filter that is too greedy is visible before it
///     eats a week of posts;
///   * it shows the transformed text, which is the only honest way to check a
///     cleanup setting.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../core/models.dart';
import '../../core/theme.dart';
import '../../l10n/app_messages.dart';

/// Opens the simulator for one task. [ruleId] must be an existing task: there is
/// nothing to evaluate before the server has stored the settings.
Future<void> showRuleSimulatorSheet(
  BuildContext context, {
  required String ruleId,
  required String ruleLabel,
  String initialChatId = '',
}) {
  return showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    builder: (_) => RuleSimulatorSheet(
      ruleId: ruleId,
      ruleLabel: ruleLabel,
      initialChatId: initialChatId,
    ),
  );
}

/// Media kinds the engine distinguishes. `none` is the absence of media, not a
/// kind, so it never reaches the request body.
const List<(String, String)> kSimulatorMediaTypes = [
  ('none', 'rules_simulate_media_none'),
  ('photo', 'rules_simulate_media_photo'),
  ('video', 'rules_simulate_media_video'),
  ('document', 'rules_simulate_media_document'),
  ('audio', 'rules_simulate_media_audio'),
];

class RuleSimulatorSheet extends StatefulWidget {
  const RuleSimulatorSheet({
    super.key,
    required this.ruleId,
    required this.ruleLabel,
    this.initialChatId = '',
  });

  final String ruleId;
  final String ruleLabel;
  final String initialChatId;

  @override
  State<RuleSimulatorSheet> createState() => _RuleSimulatorSheetState();
}

class _RuleSimulatorSheetState extends State<RuleSimulatorSheet> {
  final TextEditingController _textCtrl = TextEditingController();
  final TextEditingController _chatCtrl = TextEditingController();

  String _mediaType = 'none';
  bool _running = false;
  String? _error;
  RuleSimulation? _result;

  @override
  void initState() {
    super.initState();
    _chatCtrl.text = widget.initialChatId;
  }

  @override
  void dispose() {
    _textCtrl.dispose();
    _chatCtrl.dispose();
    super.dispose();
  }

  Future<void> _run() async {
    final t = AppMessages.of(context);
    final text = _textCtrl.text.trim();
    if (text.isEmpty) {
      setState(() => _error = t.get('rules_simulate_empty'));
      return;
    }
    setState(() {
      _running = true;
      _error = null;
    });
    final client = context.read<AppState>().buildApiClient();
    try {
      final result = await client.simulateRule(
        ruleId: widget.ruleId,
        sampleText: _textCtrl.text,
        sampleChatId: _chatCtrl.text.trim(),
        sampleHasMedia: _mediaType != 'none',
        sampleMediaType: _mediaType == 'none' ? '' : _mediaType,
      );
      if (!mounted) return;
      setState(() => _result = result);
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      setState(
        () => _error = t.get(
          'rules_simulate_failed',
          params: {'error': exc.error.message},
        ),
      );
    } catch (exc) {
      if (!mounted) return;
      setState(
        () =>
            _error = t.get('rules_simulate_failed', params: {'error': '$exc'}),
      );
    } finally {
      if (mounted) setState(() => _running = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final height = MediaQuery.sizeOf(context).height;
    return SafeArea(
      child: ConstrainedBox(
        constraints: BoxConstraints(maxHeight: height * 0.92),
        child: Padding(
          padding: EdgeInsets.only(
            bottom: MediaQuery.of(context).viewInsets.bottom,
          ),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Padding(
                padding: const EdgeInsetsDirectional.fromSTEB(
                  AppSpacing.lg,
                  0,
                  AppSpacing.sm,
                  4,
                ),
                child: Row(
                  children: [
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            t.get('rules_simulate_title'),
                            style: const TextStyle(
                              fontSize: 16,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                          Text(
                            widget.ruleLabel,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              fontSize: 12,
                              color: Colors.grey,
                            ),
                          ),
                        ],
                      ),
                    ),
                    IconButton(
                      icon: const Icon(Icons.close),
                      tooltip: t.get('action_close'),
                      onPressed: () => Navigator.of(context).pop(),
                    ),
                  ],
                ),
              ),
              const Divider(height: 1),
              Expanded(
                child: ListView(
                  padding: const EdgeInsetsDirectional.fromSTEB(
                    AppSpacing.lg,
                    AppSpacing.md,
                    AppSpacing.lg,
                    AppSpacing.md,
                  ),
                  children: [
                    Text(
                      t.get('rules_simulate_hint'),
                      style: const TextStyle(fontSize: 11, color: Colors.grey),
                    ),
                    const SizedBox(height: AppSpacing.md),
                    TextField(
                      key: const ValueKey<String>('simulate_sample'),
                      controller: _textCtrl,
                      maxLines: 4,
                      minLines: 3,
                      style: const TextStyle(fontSize: 13),
                      decoration: InputDecoration(
                        labelText: t.get('rules_simulate_sample'),
                        hintText: t.get('rules_simulate_sample_hint'),
                        border: const OutlineInputBorder(),
                      ),
                    ),
                    const SizedBox(height: AppSpacing.md),
                    TextField(
                      key: const ValueKey<String>('simulate_chat'),
                      controller: _chatCtrl,
                      style: const TextStyle(fontSize: 13),
                      decoration: InputDecoration(
                        isDense: true,
                        labelText: t.get('rules_simulate_sample'),
                        prefixIcon: const Icon(Icons.tag, size: 18),
                        border: const OutlineInputBorder(),
                      ),
                    ),
                    const SizedBox(height: AppSpacing.md),
                    DropdownButtonFormField<String>(
                      isExpanded: true,
                      key: const ValueKey<String>('simulate_media'),
                      initialValue: _mediaType,
                      isDense: true,
                      decoration: InputDecoration(
                        isDense: true,
                        labelText: t.get('rules_simulate_media_type'),
                        border: const OutlineInputBorder(),
                      ),
                      items: [
                        for (final entry in kSimulatorMediaTypes)
                          DropdownMenuItem(
                            value: entry.$1,
                            child: Text(t.get(entry.$2)),
                          ),
                      ],
                      onChanged: (v) =>
                          setState(() => _mediaType = v ?? 'none'),
                    ),
                    const SizedBox(height: AppSpacing.md),
                    if (_error != null)
                      Padding(
                        padding: const EdgeInsets.only(bottom: AppSpacing.sm),
                        child: Text(
                          _error!,
                          style: const TextStyle(
                            fontSize: 12,
                            color: Colors.red,
                          ),
                        ),
                      ),
                    FilledButton.icon(
                      key: const ValueKey<String>('simulate_run'),
                      onPressed: _running ? null : _run,
                      icon: _running
                          ? const SizedBox(
                              width: 14,
                              height: 14,
                              child: CircularProgressIndicator(strokeWidth: 2),
                            )
                          : const Icon(Icons.science_outlined, size: 18),
                      label: Text(t.get('rules_simulate_run')),
                    ),
                    if (_result != null) ...[
                      const SizedBox(height: AppSpacing.lg),
                      _verdict(t, _result!),
                    ],
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  /// The verdict card. Everything shown here is the server's answer: the reason
  /// is its string, the transformed text is its output, and the targets are the
  /// ones it would fan out to.
  Widget _verdict(AppMessages t, RuleSimulation sim) {
    final forwarded = sim.matched && !sim.loopDetected;
    final color = forwarded ? AppTheme.success : AppTheme.warning;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Container(
          width: double.infinity,
          padding: const EdgeInsets.all(AppSpacing.md),
          decoration: BoxDecoration(
            color: color.withValues(alpha: 0.12),
            borderRadius: AppRadii.allMd,
          ),
          child: Row(
            children: [
              Icon(
                forwarded ? Icons.check_circle_outline : Icons.block_outlined,
                size: 18,
                color: color,
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  forwarded
                      ? t.get('rules_simulate_forward')
                      : t.get('rules_simulate_skip'),
                  style: const TextStyle(
                    fontSize: 13,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
            ],
          ),
        ),
        const SizedBox(height: AppSpacing.sm),
        _line(
          t.get('rules_simulate_reason', params: {'reason': sim.matchReason}),
        ),
        if (sim.loopDetected && sim.loopReason.isNotEmpty)
          _line(
            t.get('rules_simulate_reason', params: {'reason': sim.loopReason}),
          ),
        if (sim.deliveryTargets.isNotEmpty)
          _line(
            t.get(
              'rules_simulate_targets',
              params: {'targets': sim.deliveryTargets.join(', ')},
            ),
          ),
        if (sim.renderedText.isNotEmpty) ...[
          const SizedBox(height: AppSpacing.sm),
          Text(
            t.get('rules_simulate_output'),
            style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w700),
          ),
          const SizedBox(height: 4),
          Container(
            width: double.infinity,
            padding: const EdgeInsets.all(AppSpacing.sm),
            decoration: BoxDecoration(
              color: Theme.of(context).colorScheme.surfaceContainerHighest,
              borderRadius: AppRadii.allSm,
            ),
            child: SelectableText(
              sim.renderedText,
              style: const TextStyle(fontSize: 12),
            ),
          ),
        ],
      ],
    );
  }

  Widget _line(String text) => Padding(
    padding: const EdgeInsets.only(bottom: 2),
    child: Text(text, style: const TextStyle(fontSize: 12)),
  );
}
