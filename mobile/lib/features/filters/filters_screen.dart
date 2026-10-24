/// Filter rules management screen (whitelist / blacklist).
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
    // One field per list, matching what the engine stores. The field holds the
    // entries comma/newline separated, so a paste of "spam, scam" or one entry
    // per line both work; a phrase with spaces stays a single entry.
    final blCtrl = TextEditingController(
      text: existing?.blacklistKeywords.join(', ') ?? '',
    );
    final wlCtrl = TextEditingController(
      text: existing?.whitelistKeywords.join(', ') ?? '',
    );
    final nameCtrl = TextEditingController(text: existing?.name ?? '');

    final saved = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
          title: Text(
            existing == null ? t.get('filters_title') : t.get('action_edit'),
          ),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(
                controller: nameCtrl,
                decoration: InputDecoration(
                  labelText: t.get('filters_name'),
                  hintText: t.get('filters_name_hint'),
                ),
              ),
              const SizedBox(height: 16),
              TextField(
                controller: blCtrl,
                autofocus: existing == null,
                decoration: InputDecoration(
                  labelText: t.get('filters_blacklist_label'),
                  hintText: t.get('filters_pattern_hint'),
                ),
              ),
              const SizedBox(height: 8),
              Align(
                alignment: Alignment.centerLeft,
                child: Text(
                  t.get('filters_words_matching_help'),
                  style: Theme.of(context).textTheme.bodySmall?.copyWith(
                        color: Theme.of(context).colorScheme.onSurfaceVariant,
                      ),
                ),
              ),
              const SizedBox(height: 16),
              TextField(
                controller: wlCtrl,
                decoration: InputDecoration(
                  labelText: t.get('filters_whitelist_label'),
                  hintText: t.get('filters_pattern_hint'),
                ),
              ),
              const SizedBox(height: 8),
              Align(
                alignment: Alignment.centerLeft,
                child: Text(
                  t.get('filters_words_matching_help'),
                  style: Theme.of(context).textTheme.bodySmall?.copyWith(
                        color: Theme.of(context).colorScheme.onSurfaceVariant,
                      ),
                ),
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
                // The model sends the lists the proto actually carries; a
                // multi-word phrase survives because splitKeywordList never
                // breaks an entry on a space.
                final blacklist = splitKeywordList(blCtrl.text);
                final whitelist = splitKeywordList(wlCtrl.text);
                if (blacklist.isEmpty && whitelist.isEmpty) return;
                final name = nameCtrl.text.trim().isNotEmpty
                    ? nameCtrl.text.trim()
                    : (blacklist.isNotEmpty
                        ? t.get('filters_kind_blacklist')
                        : t.get('filters_kind_whitelist'));
                final client = context.read<AppState>().buildApiClient();
                if (existing == null) {
                  await client.createFilter(
                    FilterRule(
                      id: '',
                      name: name,
                      blacklistKeywords: blacklist,
                      whitelistKeywords: whitelist,
                    ),
                  );
                } else {
                  await client.updateFilter(
                    existing.id,
                    existing.copyWith(
                      name: name,
                      blacklistKeywords: blacklist,
                      whitelistKeywords: whitelist,
                    ),
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
        content: Text(filter.keywordEntries.isNotEmpty
            ? filter.keywordEntries.join(', ')
            : filter.name),
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
              Padding(
                padding: const EdgeInsets.all(AppSpacing.lg),
                child: InlineBanner(
                  message: _error!,
                  onRetry: _load,
                  retryLabel: t.get('action_retry'),
                ),
              ),
            Expanded(
              child: _loading && _filters.isEmpty
                  ? const LoadingList(rows: 6, itemExtent: 76)
                  : _filters.isEmpty
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
                              f.name.isNotEmpty
                                  ? f.name
                                  : (f.keywordEntries.isNotEmpty
                                      ? f.keywordEntries.join(', ')
                                      : t.get('filters_empty')),
                              style: const TextStyle(
                                fontWeight: FontWeight.bold,
                              ),
                            ),
                            subtitle: Text(
                              t.get(
                                'filters_kind_summary',
                                params: {'kind': _kindLabel(t, f)},
                              ),
                            ),
                            onTap: () => _addOrEdit(f),
                            trailing: IconButton(
                              tooltip: t.get('action_delete'),
                              icon: Icon(
                                Icons.delete_outline,
                                color: AppColors.of(context).danger,
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
/// The proto carries no `kind` string: a filter is a blacklist, a whitelist,
/// or both, according to which list holds entries. The label says which, so a
/// user can see at a glance whether the entries block or allow.
String _kindLabel(AppMessages t, FilterRule f) {
  final hasBl = f.blacklistKeywords.isNotEmpty;
  final hasWl = f.whitelistKeywords.isNotEmpty;
  if (hasBl && hasWl) return t.get('filters_kind_both');
  if (hasBl) return t.get('filters_kind_blacklist');
  if (hasWl) return t.get('filters_kind_whitelist');
  return t.get('filters_kind_empty');
}
