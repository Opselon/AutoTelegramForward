/// AI rewriting configuration screen.
///
/// Security: The mobile client never receives or stores raw provider API keys.
/// A key typed into the editor is sent once, on save; the check action probes
/// the endpoint and key on the server, so the key never leaves this device
/// towards the provider directly.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../core/models.dart';
import '../../core/theme.dart';
import '../../l10n/app_messages.dart';
import '../../widgets/app_ui.dart';
import '../../widgets/loading_list.dart';
import 'ai_config_editor_dialog.dart';

class AiScreen extends StatefulWidget {
  const AiScreen({super.key});

  @override
  State<AiScreen> createState() => _AiScreenState();
}

class _AiScreenState extends State<AiScreen> {
  List<AiConfig> _configs = const [];

  /// The provider catalogue from the core. An older server (or a failed fetch)
  /// leaves this empty and the editor falls back to its static list — an
  /// optional surface must never blank the screen.
  List<AiProviderInfo> _providers = const [];
  bool _loading = false;
  String? _error;

  /// One-shot guard: `didChangeDependencies` can fire more than once, and a
  /// second fetch on every theme/locale change would hammer the API.
  bool _bootstrapped = false;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (_bootstrapped) return;
    _bootstrapped = true;
    // Must run here, not in initState: this screen reads AppMessages (via
    // Localizations) and AppState (via Provider) — both are inherited widgets,
    // and depending on them before initState completes throws.
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
      final providers = await _providersOrEmpty(client);
      if (!mounted) return;
      setState(() {
        _configs = list;
        _providers = providers;
      });
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      setState(
        () => _error = exc.error.message.isNotEmpty
            ? exc.error.message
            : t.get('state_error'),
      );
    } catch (exc) {
      // Never swallow: a network failure must not look like "no data".
      if (!mounted) return;
      setState(
        () => _error =
            '${t.get('state_error')} '
            '(${exc.toString().split('(').first.trim()})',
      );
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  /// Fetching the catalogue is advisory: its failure yields an empty list so the
  /// saved configs still render.
  Future<List<AiProviderInfo>> _providersOrEmpty(ApiClient client) async {
    try {
      return await client.listAiProviders();
    } catch (_) {
      return const [];
    }
  }

  Future<void> _delete(AiConfig config) async {
    final t = AppMessages.of(context);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(t.get('ai_delete_title')),
        content: Text('${config.provider} • ${config.model}'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: Text(t.get('ai_cancel')),
          ),
          ElevatedButton(
            style: ElevatedButton.styleFrom(backgroundColor: Colors.red),
            onPressed: () => Navigator.pop(ctx, true),
            child: Text(t.get('ai_delete_action')),
          ),
        ],
      ),
    );

    if (confirmed != true || !mounted) return;

    final client = context.read<AppState>().buildApiClient();
    try {
      await client.deleteAiConfig(config.id);
      await _load();
    } on ApiException catch (exc) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(exc.error.message)));
    } catch (exc) {
      // Deletion failed: tell the user instead of vanishing silently.
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            '${t.get('ai_delete_failed')} '
            '(${exc.toString().split('(').first.trim()})',
          ),
        ),
      );
    }
  }

  Future<void> _openEditor(AiConfig? existing) async {
    final client = context.read<AppState>().buildApiClient();
    final result = await showDialog<AiConfig>(
      context: context,
      builder: (_) => AiConfigEditorDialog(
        existing: existing,
        providers: _providers,
        client: client,
      ),
    );
    if (result == null || !mounted) return;

    final t = AppMessages.of(context);
    try {
      if (existing == null) {
        await client.createAiConfig(result);
      } else {
        await client.updateAiConfig(existing.id, result);
      }
      await _load();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            t.get(existing == null ? 'ai_saved_created' : 'ai_saved_updated'),
          ),
        ),
      );
    } on ApiException catch (exc) {
      if (!mounted) return;
      // A refused endpoint must name itself: `endpoint_blocked` is not a
      // sentence, and it is the one refusal the user can fix.
      final reasonKey = aiSaveFailureKey(exc.error.message);
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            reasonKey.isNotEmpty ? t.get(reasonKey) : exc.error.message,
          ),
        ),
      );
    } catch (exc) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            '${t.get('ai_save_failed')} '
            '(${exc.toString().split('(').first.trim()})',
          ),
        ),
      );
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
      floatingActionButton: FloatingActionButton.extended(
        icon: const Icon(Icons.add),
        label: Text(t.get('ai_form_new')),
        onPressed: () => _openEditor(null),
      ),
      body: RefreshIndicator(
        onRefresh: _load,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            if (_error != null)
              Padding(
                padding: const EdgeInsets.all(AppSpacing.lg),
                child: InlineBanner(
                  message: _error!,
                  onRetry: _load,
                  retryLabel: t.get('action_retry'),
                ),
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

            if (_loading && _configs.isEmpty)
              const LoadingList(rows: 4, itemExtent: 96, showAvatar: false)
            else if (_configs.isEmpty)
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
                      style: Theme.of(context).textTheme.bodyMedium
                          ?.copyWith(color: Colors.grey),
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
                      '${t.get('ai_target_label')}: '
                      '${c.targetLanguage.isNotEmpty ? c.targetLanguage : t.get('ai_target_same')}'
                      ' • ${c.isEnabled ? t.get('ai_active_yes') : t.get('ai_active_no')}'
                      '\n${t.get('ai_endpoint_label')}: '
                      '${c.baseUrl.isNotEmpty ? c.baseUrl : t.get('ai_endpoint_none')}',
                    ),
                    isThreeLine: true,
                    onTap: () => _openEditor(c),
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
