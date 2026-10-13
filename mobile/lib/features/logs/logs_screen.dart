/// Operational logs explorer.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/app_state.dart';
import '../../core/models.dart';
import '../../l10n/app_messages.dart';

class LogsScreen extends StatefulWidget {
  const LogsScreen({super.key});

  @override
  State<LogsScreen> createState() => _LogsScreenState();
}

class _LogsScreenState extends State<LogsScreen> {
  List<LogEntry> _logs = const [];
  bool _loading = false;
  String _levelFilter = '';

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() => _loading = true);
    final client = context.read<AppState>().buildApiClient();
    try {
      final list = await client.queryLogs(limit: 50, level: _levelFilter);
      if (!mounted) return;
      setState(() => _logs = list);
    } catch (_) {
      // Handled
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);

    return Scaffold(
      appBar: AppBar(
        title: Text(t.get('logs_title')),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            tooltip: t.get('action_refresh'),
            onPressed: _loading ? null : _load,
          ),
        ],
      ),
      body: Column(
        children: [
          // Filter Chips
          SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
            child: Row(
              children: [
                ChoiceChip(
                  label: const Text('All'),
                  selected: _levelFilter.isEmpty,
                  onSelected: (val) {
                    if (val) {
                      setState(() => _levelFilter = '');
                      _load();
                    }
                  },
                ),
                const SizedBox(width: 8),
                ChoiceChip(
                  label: const Text('Errors'),
                  selected: _levelFilter == 'error',
                  onSelected: (val) {
                    if (val) {
                      setState(() => _levelFilter = 'error');
                      _load();
                    }
                  },
                ),
                const SizedBox(width: 8),
                ChoiceChip(
                  label: const Text('Warnings'),
                  selected: _levelFilter == 'warning',
                  onSelected: (val) {
                    if (val) {
                      setState(() => _levelFilter = 'warning');
                      _load();
                    }
                  },
                ),
                const SizedBox(width: 8),
                ChoiceChip(
                  label: const Text('Info'),
                  selected: _levelFilter == 'info',
                  onSelected: (val) {
                    if (val) {
                      setState(() => _levelFilter = 'info');
                      _load();
                    }
                  },
                ),
              ],
            ),
          ),
          const Divider(height: 1),

          Expanded(
            child: RefreshIndicator(
              onRefresh: _load,
              child: _logs.isEmpty && !_loading
                  ? Center(child: Text(t.get('logs_empty')))
                  : ListView.separated(
                      padding: const EdgeInsets.all(16),
                      itemCount: _logs.length,
                      separatorBuilder: (_, __) => const Divider(height: 1),
                      itemBuilder: (context, index) {
                        final log = _logs[index];
                        final isErr = log.level == 'error';
                        final isWarn = log.level == 'warning';
                        return ListTile(
                          leading: Icon(
                            isErr
                                ? Icons.error
                                : (isWarn ? Icons.warning : Icons.info_outline),
                            color: isErr
                                ? Colors.red
                                : (isWarn ? Colors.amber : Colors.blue),
                            size: 20,
                          ),
                          title: Text(log.message, style: const TextStyle(fontSize: 14)),
                          subtitle: Text(
                            '${log.category} • ${log.timestamp}',
                            style: const TextStyle(fontSize: 12, color: Colors.grey),
                          ),
                        );
                      },
                    ),
            ),
          ),
        ],
      ),
    );
  }
}
