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
    // Regex patterns are one per line: a pattern may contain a space, and a
    // comma is legal inside one, so the keyword separators cannot be reused.
    final rxCtrl = TextEditingController(
      text: existing?.regexPatterns.join('\n') ?? '',
    );
    final nameCtrl = TextEditingController(text: existing?.name ?? '');

    final saved = await showDialog<bool>(
      context: context,
      builder: (ctx) => _FilterEditor(
        existing: existing,
        nameCtrl: nameCtrl,
        blCtrl: blCtrl,
        wlCtrl: wlCtrl,
        rxCtrl: rxCtrl,
      ),
    );

    if (saved == true) {
      await _load();
    }
  }

  /// The read-only preview: the server's canonical matcher decides whether a
  /// sample message would pass this filter. Nothing is saved and nothing is
  /// forwarded — the endpoint is a test, not a delivery path.
  Future<void> _testRule(FilterRule filter) async {
    final t = AppMessages.of(context);
    final sample = await showDialog<String>(
      context: context,
      builder: (ctx) => _FilterTestDialog(rule: filter),
    );
    if (sample == null || !mounted) return;

    final client = context.read<AppState>().buildApiClient();
    try {
      final result = await client.testFilter(filter.id, sample);
      if (!mounted) return;
      await showDialog<void>(
        context: context,
        builder: (ctx) => AlertDialog(
          title: Text(t.get('filters_test_title')),
          content: Text(_testSummary(t, result)),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(ctx),
              child: Text(t.get('action_close')),
            ),
          ],
        ),
      );
    } on ApiException catch (exc) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            t.get('filters_test_error', params: {'error': exc.error.message}),
          ),
        ),
      );
    } catch (exc) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            t.get(
              'filters_test_error',
              params: {'error': exc.toString().split('(').first.trim()},
            ),
          ),
        ),
      );
    }
  }

  Future<void> _delete(FilterRule filter) async {
    final t = AppMessages.of(context);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(t.get('filters_delete_confirm')),
        content: Text(
          filter.keywordEntries.isNotEmpty
              ? filter.keywordEntries.join(', ')
              : filter.name,
        ),
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
                            trailing: Row(
                              mainAxisSize: MainAxisSize.min,
                              children: [
                                IconButton(
                                  tooltip: t.get('filters_test_run'),
                                  icon: Icon(
                                    Icons.science_outlined,
                                    color: AppColors.of(context).primary,
                                  ),
                                  onPressed: () => _testRule(f),
                                ),
                                IconButton(
                                  tooltip: t.get('action_delete'),
                                  icon: Icon(
                                    Icons.delete_outline,
                                    color: AppColors.of(context).danger,
                                  ),
                                  onPressed: () => _delete(f),
                                ),
                              ],
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

/// The filter editor: the three user-facing options (Keyword / Phrase, Regex,
/// Long-Text Matching) as one picker, with a sample tester on the saved rule.
///
/// The picker is a UI workflow over the lists the proto carries — the model
/// classifies a rule from its populated lists, and the editor never persists a
/// separate `kind`. The option decides which fields are offered: Keyword and
/// Long-Text edit the word lists, Regex edits the pattern list. The mode
/// picker (ANY / ALL / PHRASE) is presented for the Long-Text workflow and is
/// resolved by the server's whitelist_mode; the client does not interpret it.
class _FilterEditor extends StatefulWidget {
  const _FilterEditor({
    required this.existing,
    required this.nameCtrl,
    required this.blCtrl,
    required this.wlCtrl,
    required this.rxCtrl,
  });

  final FilterRule? existing;
  final TextEditingController nameCtrl;
  final TextEditingController blCtrl;
  final TextEditingController wlCtrl;
  final TextEditingController rxCtrl;

  @override
  State<_FilterEditor> createState() => _FilterEditorState();
}

class _FilterEditorState extends State<_FilterEditor> {
  FilterKind _option = FilterKind.keyword;
  bool _saving = false;

  @override
  void initState() {
    super.initState();
    final kind = widget.existing?.kind ?? FilterKind.empty;
    _option = kind == FilterKind.regex ? FilterKind.regex : kind;
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final showWords = _option != FilterKind.regex;
    final showRegex = _option != FilterKind.keyword;

    return AlertDialog(
      title: Text(
        widget.existing == null ? t.get('filters_title') : t.get('action_edit'),
      ),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            TextField(
              controller: widget.nameCtrl,
              decoration: InputDecoration(
                labelText: t.get('filters_name'),
                hintText: t.get('filters_name_hint'),
              ),
            ),
            const SizedBox(height: 16),
            Text(
              t.get('filters_three_options_title'),
              style: Theme.of(context).textTheme.bodySmall?.copyWith(
                fontWeight: FontWeight.w600,
                color: Theme.of(context).colorScheme.onSurfaceVariant,
              ),
            ),
            for (final option in FilterKind.values)
              if (option != FilterKind.mixed)
                RadioListTile<FilterKind>(
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  value: option,
                  groupValue: _option,
                  title: Text(_optionTitle(t, option)),
                  subtitle: Text(_optionSub(t, option)),
                  onChanged: (v) {
                    if (v != null) setState(() => _option = v);
                  },
                ),
            if (showWords) ...[
              const SizedBox(height: 8),
              TextField(
                controller: widget.blCtrl,
                decoration: InputDecoration(
                  labelText: t.get('filters_blacklist_label'),
                  hintText: t.get('filters_pattern_hint'),
                ),
              ),
              const SizedBox(height: 16),
              TextField(
                controller: widget.wlCtrl,
                decoration: InputDecoration(
                  labelText: t.get('filters_whitelist_label'),
                  hintText: t.get('filters_pattern_hint'),
                ),
              ),
              const SizedBox(height: 8),
              Align(
                alignment: Alignment.centerLeft,
                child: Text(
                  _option == FilterKind.empty
                      ? t.get('filters_option_longtext_sub')
                      : t.get('filters_words_matching_help'),
                  style: Theme.of(context).textTheme.bodySmall?.copyWith(
                    color: Theme.of(context).colorScheme.onSurfaceVariant,
                  ),
                ),
              ),
            ],
            if (showRegex) ...[
              const SizedBox(height: 16),
              TextField(
                controller: widget.rxCtrl,
                maxLines: 3,
                decoration: InputDecoration(
                  labelText: t.get('filters_regex_label'),
                  hintText: t.get('filters_regex_hint'),
                ),
              ),
            ],
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context, false),
          child: Text(t.get('action_cancel')),
        ),
        ElevatedButton(
          onPressed: _saving ? null : _save,
          child: Text(t.get('action_save')),
        ),
      ],
    );
  }

  String _optionTitle(AppMessages t, FilterKind kind) {
    switch (kind) {
      case FilterKind.keyword:
        return t.get('filters_option_keyword');
      case FilterKind.regex:
        return t.get('filters_option_regex');
      case FilterKind.empty:
        return t.get('filters_option_longtext');
      case FilterKind.mixed:
        return _optionLabel(t, kind);
    }
  }

  String _optionSub(AppMessages t, FilterKind kind) {
    switch (kind) {
      case FilterKind.keyword:
        return t.get('filters_option_keyword_sub');
      case FilterKind.regex:
        return t.get('filters_option_regex_sub');
      case FilterKind.empty:
        return t.get('filters_option_longtext_sub');
      case FilterKind.mixed:
        return '';
    }
  }

  Future<void> _save() async {
    final t = AppMessages.of(context);
    final blacklist = splitKeywordList(widget.blCtrl.text);
    final whitelist = splitKeywordList(widget.wlCtrl.text);
    // One pattern per line: the field's own separator, because a comma can
    // legitimately appear inside a pattern.
    final regex = widget.rxCtrl.text
        .split('\n')
        .map((e) => e.trim())
        .where((e) => e.isNotEmpty)
        .toList(growable: false);
    if (blacklist.isEmpty && whitelist.isEmpty && regex.isEmpty) return;

    setState(() => _saving = true);
    try {
      final name = widget.nameCtrl.text.trim().isNotEmpty
          ? widget.nameCtrl.text.trim()
          : (blacklist.isNotEmpty
                ? t.get('filters_kind_blacklist')
                : t.get('filters_kind_whitelist'));
      final client = context.read<AppState>().buildApiClient();
      if (widget.existing == null) {
        await client.createFilter(
          FilterRule(
            id: '',
            name: name,
            blacklistKeywords: blacklist,
            whitelistKeywords: whitelist,
            regexPatterns: regex,
          ),
        );
      } else {
        await client.updateFilter(
          widget.existing!.id,
          widget.existing!.copyWith(
            name: name,
            blacklistKeywords: blacklist,
            whitelistKeywords: whitelist,
            regexPatterns: regex,
          ),
        );
      }
      if (mounted) Navigator.pop(context, true);
    } catch (exc) {
      // A failed save must not look like a success: the dialog stays open and
      // the error reaches the user instead of vanishing.
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(exc.toString().split('(').first.trim())),
        );
      }
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }
}

/// The sample-message tester for one saved filter.
///
/// The verdict comes from the server's canonical matcher, so it can never
/// disagree with what the production pipeline will do with the same message.
class _FilterTestDialog extends StatefulWidget {
  const _FilterTestDialog({required this.rule});

  final FilterRule rule;

  @override
  State<_FilterTestDialog> createState() => _FilterTestDialogState();
}

class _FilterTestDialogState extends State<_FilterTestDialog> {
  final _ctrl = TextEditingController();
  bool _busy = false;
  FilterTestResult? _result;

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);

    return AlertDialog(
      title: Text(t.get('filters_test_title')),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              _optionLabel(t, widget.rule.kind),
              style: Theme.of(context).textTheme.bodySmall
                  ?.copyWith(fontWeight: FontWeight.w600),
            ),
            const SizedBox(height: 8),
            TextField(
              controller: _ctrl,
              maxLines: 5,
              autofocus: true,
              decoration: InputDecoration(
                hintText: t.get('filters_test_sample_hint'),
                border: const OutlineInputBorder(),
              ),
            ),
            if (_result != null) ...[
              const SizedBox(height: 12),
              Text(_testSummary(t, _result!)),
            ],
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: Text(t.get('action_close')),
        ),
        ElevatedButton(
          onPressed: _busy ? null : _run,
          child: Text(t.get('filters_test_run')),
        ),
      ],
    );
  }

  Future<void> _run() async {
    final sample = _ctrl.text.trim();
    if (sample.isEmpty || !mounted) return;
    setState(() {
      _busy = true;
      _result = null;
    });
    try {
      final client = context.read<AppState>().buildApiClient();
      final result = await client.testFilter(widget.rule.id, sample);
      if (mounted) setState(() => _result = result);
    } on ApiException catch (exc) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(
              t_testError(AppMessages.of(context), exc.error.message),
            ),
          ),
        );
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }
}

/// A localized API-error line for the tester's failure path.
String t_testError(AppMessages t, String error) =>
    t.get('filters_test_error', params: {'error': error});
