/// AI rewriting configuration screen.
///
/// Security: The mobile client never receives or stores raw provider API keys.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../core/models.dart';
import '../../l10n/app_messages.dart';

class AiScreen extends StatefulWidget {
  const AiScreen({super.key});

  @override
  State<AiScreen> createState() => _AiScreenState();
}

class _AiScreenState extends State<AiScreen> {
  List<AiConfig> _configs = const [];
  bool _loading = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });

    final client = context.read<AppState>().buildApiClient();
    final t = AppMessages.of(context);

    try {
      final list = await client.listAiConfigs();
      if (!mounted) return;
      setState(() => _configs = list);
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      setState(
        () =>
            _error =
                exc.error.message.isNotEmpty
                    ? exc.error.message
                    : t.get('state_error'),
      );
    } catch (_) {
      if (!mounted) return;
      setState(() => _error = t.get('state_error'));
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _delete(AiConfig config) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder:
          (ctx) => AlertDialog(
            title: const Text('Delete AI configuration?'),
            content: Text('${config.provider} • ${config.model}'),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(ctx, false),
                child: const Text('Cancel'),
              ),
              ElevatedButton(
                style: ElevatedButton.styleFrom(backgroundColor: Colors.red),
                onPressed: () => Navigator.pop(ctx, true),
                child: const Text('Delete'),
              ),
            ],
          ),
    );

    if (confirmed != true || !mounted) return;

    final client = context.read<AppState>().buildApiClient();
    try {
      await client.deleteAiConfig(config.id);
      await _load();
    } catch (_) {
      // Deletion error handled
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);

    return Scaffold(
      appBar: AppBar(
        title: Text(t.get('ai_title')),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            onPressed: _loading ? null : _load,
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: _load,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            if (_error != null)
              Container(
                padding: const EdgeInsets.all(12),
                margin: const EdgeInsets.only(bottom: 16),
                decoration: BoxDecoration(
                  color: Colors.red.withValues(alpha: 0.15),
                  borderRadius: BorderRadius.circular(8),
                  border: Border.all(color: Colors.red),
                ),
                child: Text(_error!, style: const TextStyle(color: Colors.red)),
              ),
            // Notice about API keys security
            Container(
              padding: const EdgeInsets.all(12),
              margin: const EdgeInsets.only(bottom: 16),
              decoration: BoxDecoration(
                color: const Color(0xFF06B6D4).withValues(alpha: 0.15),
                borderRadius: BorderRadius.circular(8),
                border: Border.all(color: const Color(0xFF06B6D4)),
              ),
              child: Row(
                children: [
                  const Icon(Icons.info_outline, color: Color(0xFF06B6D4)),
                  const SizedBox(width: 8),
                  Expanded(child: Text(t.get('ai_note'))),
                ],
              ),
            ),

            if (_configs.isEmpty && !_loading)
              Padding(
                padding: const EdgeInsets.symmetric(
                  horizontal: 32,
                  vertical: 48,
                ),
                child: Column(
                  children: [
                    const Icon(
                      Icons.psychology_outlined,
                      size: 64,
                      color: Colors.grey,
                    ),
                    const SizedBox(height: 16),
                    Text(
                      t.get('ai_empty'),
                      textAlign: TextAlign.center,
                      style: Theme.of(
                        context,
                      ).textTheme.bodyMedium?.copyWith(color: Colors.grey),
                    ),
                  ],
                ),
              )
            else
              ..._configs.map((c) {
                return Card(
                  child: ListTile(
                    leading: const CircleAvatar(
                      backgroundColor: Color(0xFF06B6D4),
                      child: Icon(
                        Icons.auto_awesome,
                        color: Colors.white,
                        size: 20,
                      ),
                    ),
                    title: Text(
                      '${c.provider.toUpperCase()} (${c.model})',
                      style: const TextStyle(fontWeight: FontWeight.bold),
                    ),
                    subtitle: Text(
                      'Target: ${c.targetLanguage.isNotEmpty ? c.targetLanguage : 'same'} • Active: ${c.isEnabled ? "yes" : "no"}',
                    ),
                    trailing: IconButton(
                      icon: const Icon(Icons.delete_outline, color: Colors.red),
                      onPressed: () => _delete(c),
                    ),
                  ),
                );
              }),
          ],
        ),
      ),
    );
  }
}
