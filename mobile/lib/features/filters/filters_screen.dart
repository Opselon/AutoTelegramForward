/// Filter rules management screen (whitelist / blacklist).
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../core/models.dart';
import '../../l10n/app_messages.dart';

class FiltersScreen extends StatefulWidget {
  const FiltersScreen({super.key});

  @override
  State<FiltersScreen> createState() => _FiltersScreenState();
}

class _FiltersScreenState extends State<FiltersScreen> {
  List<FilterRule> _filters = const [];
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
      final list = await client.listFilters();
      if (!mounted) return;
      setState(() => _filters = list);
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

  Future<void> _addOrEdit([FilterRule? existing]) async {
    final t = AppMessages.of(context);
    final patternCtrl = TextEditingController(text: existing?.pattern ?? '');
    String kind = existing?.kind ?? 'blacklist';

    final saved = await showDialog<bool>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          title: Text(
            existing == null ? t.get('filters_title') : t.get('action_edit'),
          ),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(
                controller: patternCtrl,
                autofocus: true,
                decoration: InputDecoration(
                  labelText: t.get('filters_pattern'),
                  hintText: t.get('filters_pattern_hint'),
                ),
              ),
              const SizedBox(height: 16),
              DropdownButtonFormField<String>(
                initialValue: kind,
                decoration: InputDecoration(labelText: t.get('filters_kind')),
                items: [
                  DropdownMenuItem(
                    value: 'blacklist',
                    child: Text(t.get('filters_kind_blacklist')),
                  ),
                  DropdownMenuItem(
                    value: 'whitelist',
                    child: Text(t.get('filters_kind_whitelist')),
                  ),
                  DropdownMenuItem(
                    value: 'link',
                    child: Text(t.get('filters_kind_link')),
                  ),
                ],
                onChanged: (v) {
                  if (v != null) setDialogState(() => kind = v);
                },
              ),
            ],
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(ctx, false),
              child: Text(t.get('action_cancel')),
            ),
            ElevatedButton(
              onPressed: () async {
                final pattern = patternCtrl.text.trim();
                if (pattern.isEmpty) return;
                final client = context.read<AppState>().buildApiClient();
                if (existing == null) {
                  await client.createFilter(
                    FilterRule(id: '', pattern: pattern, kind: kind),
                  );
                } else {
                  await client.updateFilter(
                    existing.id,
                    existing.copyWith(pattern: pattern, kind: kind),
                  );
                }
                if (ctx.mounted) Navigator.pop(ctx, true);
              },
              child: Text(t.get('action_save')),
            ),
          ],
        ),
      ),
    );

    if (saved == true) {
      await _load();
    }
  }

  Future<void> _delete(FilterRule filter) async {
    final t = AppMessages.of(context);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(t.get('filters_delete_confirm')),
        content: Text(filter.pattern),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: Text(t.get('action_cancel')),
          ),
          ElevatedButton(
            style: ElevatedButton.styleFrom(backgroundColor: Colors.red),
            onPressed: () => Navigator.pop(ctx, true),
            child: Text(t.get('action_delete')),
          ),
        ],
      ),
    );

    if (confirmed != true || !mounted) return;

    final client = context.read<AppState>().buildApiClient();
    try {
      await client.deleteFilter(filter.id);
      await _load();
    } on ApiException catch (exc) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(exc.error.message)));
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);

    return Scaffold(
      appBar: AppBar(
        title: Text(t.get('filters_title')),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            tooltip: t.get('action_refresh'),
            onPressed: _loading ? null : _load,
          ),
        ],
      ),
      floatingActionButton: FloatingActionButton(
        onPressed: () => _addOrEdit(),
        child: const Icon(Icons.add),
      ),
      body: RefreshIndicator(
        onRefresh: _load,
        child: Column(
          children: [
            if (_error != null)
              Container(
                padding: const EdgeInsets.all(12),
                margin: const EdgeInsets.all(16),
                decoration: BoxDecoration(
                  color: Colors.red.withValues(alpha: 0.15),
                  borderRadius: BorderRadius.circular(8),
                  border: Border.all(color: Colors.red),
                ),
                child: Text(_error!, style: const TextStyle(color: Colors.red)),
              ),
            Expanded(
              child: _filters.isEmpty && !_loading
                  ? ListView(
                      children: [
                        Padding(
                          padding: const EdgeInsets.symmetric(
                            horizontal: 32,
                            vertical: 64,
                          ),
                          child: Column(
                            mainAxisAlignment: MainAxisAlignment.center,
                            children: [
                              const Icon(
                                Icons.filter_alt_off_rounded,
                                size: 64,
                                color: Colors.grey,
                              ),
                              const SizedBox(height: 16),
                              Text(
                                t.get('filters_empty'),
                                textAlign: TextAlign.center,
                                style: Theme.of(context).textTheme.bodyMedium
                                    ?.copyWith(color: Colors.grey),
                              ),
                            ],
                          ),
                        ),
                      ],
                    )
                  : ListView.builder(
                      padding: const EdgeInsets.all(16),
                      itemCount: _filters.length,
                      itemBuilder: (context, index) {
                        final f = _filters[index];
                        return Card(
                          child: ListTile(
                            title: Text(
                              f.pattern,
                              style: const TextStyle(
                                fontWeight: FontWeight.bold,
                              ),
                            ),
                            subtitle: Text(
                              t.get(
                                'filters_kind_summary',
                                params: {'kind': _kindLabel(t, f.kind)},
                              ),
                            ),
                            onTap: () => _addOrEdit(f),
                            trailing: IconButton(
                              icon: const Icon(
                                Icons.delete_outline,
                                color: Colors.red,
                              ),
                              onPressed: () => _delete(f),
                            ),
                          ),
                        );
                      },
                    ),
            ),
          ],
        ),
      ),
    );
  }
}

/// The filter kind as the catalog names it.
///
/// `kind` arrives from the API as an identifier (`blacklist`, `whitelist`,
/// `link`); the list used to print it verbatim next to a hardcoded English
/// "Kind:" prefix. Known kinds resolve through the same keys the editor's
/// dropdown uses, and an unknown one falls back to the raw identifier rather
/// than an invented label.
String _kindLabel(AppMessages t, String kind) {
  const known = <String>{'blacklist', 'whitelist', 'link'};
  return known.contains(kind) ? t.get('filters_kind_$kind') : kind;
}
