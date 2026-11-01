import 'package:flutter/material.dart';

import '../api/agent_activity.dart';

/// Inspecting an activity group only displays journal evidence; it runs no tool.
class AgentActivityGroup extends StatelessWidget {
  const AgentActivityGroup({super.key, required this.activities});

  final List<AgentToolActivity> activities;

  String _toolName(String name) => switch (name) {
    'shell' => 'Shell',
    'read_file' => 'Read file',
    'write_file' => 'Write file',
    'search' => 'Search',
    _ => name,
  };

  @override
  Widget build(BuildContext context) {
    final color = Theme.of(context).colorScheme;
    final count = activities.length;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
      child: ExpansionTile(
        key: PageStorageKey('agent-activity-${activities.first.eventId}'),
        maintainState: true,
        tilePadding: const EdgeInsets.symmetric(horizontal: 12),
        childrenPadding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
        shape: RoundedRectangleBorder(
          side: BorderSide(color: color.outlineVariant),
          borderRadius: BorderRadius.circular(10),
        ),
        collapsedShape: RoundedRectangleBorder(
          side: BorderSide(color: color.outlineVariant),
          borderRadius: BorderRadius.circular(10),
        ),
        leading: const Icon(Icons.build_outlined, size: 19),
        title: Text(
          'Tool activity · $count ${count == 1 ? 'record' : 'records'}',
        ),
        children: [
          for (final activity in activities)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Container(
                width: double.infinity,
                decoration: BoxDecoration(
                  color: color.surfaceContainerHigh,
                  border: Border.all(color: color.outlineVariant),
                  borderRadius: BorderRadius.circular(8),
                ),
                padding: const EdgeInsets.all(12),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Wrap(
                      spacing: 12,
                      runSpacing: 4,
                      children: [
                        Text(
                          _toolName(activity.tool),
                          style: Theme.of(context).textTheme.labelLarge,
                        ),
                        Text(activity.status),
                        if (activity.exitCode != null)
                          Text('Exit code ${activity.exitCode}'),
                        if (activity.revision != null)
                          Text('Revision ${activity.revision}'),
                      ],
                    ),
                    if (activity.summary != null) ...[
                      const SizedBox(height: 8),
                      Text(
                        'Recorded summary',
                        style: Theme.of(context).textTheme.labelSmall,
                      ),
                      SelectableText(activity.summary!),
                    ],
                    if (activity.output != null) ...[
                      const SizedBox(height: 8),
                      Text(
                        'Recorded output',
                        style: Theme.of(context).textTheme.labelSmall,
                      ),
                      const SizedBox(height: 4),
                      ConstrainedBox(
                        constraints: const BoxConstraints(maxHeight: 240),
                        child: SingleChildScrollView(
                          child: SelectableText(
                            activity.output!,
                            style: Theme.of(context).textTheme.bodySmall
                                ?.copyWith(
                                  fontFamily: 'monospace',
                                  height: 1.4,
                                ),
                          ),
                        ),
                      ),
                    ],
                  ],
                ),
              ),
            ),
        ],
      ),
    );
  }
}
