import 'models.dart';

/// A read-only projection of one supervisor-authored journal record.
class AgentToolActivity {
  const AgentToolActivity({
    required this.eventId,
    required this.taskId,
    required this.tool,
    required this.status,
    this.revision,
    this.summary,
    this.output,
    this.exitCode,
  });

  final String eventId, taskId, tool, status;
  final int? revision, exitCode;
  final String? summary, output;

  static String? _boundedText(Object? value, int limit) {
    if (value is! String || value.isEmpty) return null;
    final characters = value.runes;
    if (characters.length <= limit) return value;
    return '${String.fromCharCodes(characters.take(limit))}\n…[display truncated]';
  }

  static AgentToolActivity? fromJournal(JournalEntry entry) {
    if (entry.kind != 'agent.tool' ||
        entry.origin != 'agent-supervisor' ||
        entry.eventId.isEmpty ||
        entry.taskRefs.length != 1) {
      return null;
    }
    final task = entry.payload['task_id'];
    final tool = entry.payload['tool'];
    if (task is! String ||
        task.isEmpty ||
        task.length > 128 ||
        task != entry.taskRefs.single ||
        tool is! String ||
        tool.isEmpty ||
        tool.length > 128) {
      return null;
    }
    final rawEvidence = entry.payload['evidence'];
    final evidence = rawEvidence is Map ? rawEvidence : const {};
    final rawRevision = evidence['objective_revision'];
    final rawExit = evidence['exit_code'];
    final result = entry.payload['ok'];
    final admissionOnly = entry.payload['admission_only'] == true;
    return AgentToolActivity(
      eventId: entry.eventId,
      taskId: task,
      tool: tool,
      revision: rawRevision is int && rawRevision > 0 ? rawRevision : null,
      exitCode: rawExit is int ? rawExit : null,
      status: admissionOnly
          ? 'Result not established'
          : result == true
          ? 'Recorded success'
          : result == false
          ? 'Recorded failure'
          : 'Result not established',
      summary: _boundedText(evidence['summary'], 1000),
      output: _boundedText(evidence['output'], 8000),
    );
  }
}
