/// Synthetic wire fixtures only; never loaded by application code.
Map<String, Object?> artifactComponent(
  String id,
  String type,
  Map<String, Object?> data, {
  String title = '',
}) => {'id': id, 'type': type, 'title': title, 'data': data};

Map<String, Object?> artifactFixture({
  String id = 'artifact-test',
  int submitSequence = 1,
  List<Map<String, Object?>>? components,
  int revision = 1,
  String sessionId = 'session-test',
  String threadId = 'thread-test',
  String taskId = 'task-test',
}) => {
  'schema_version': '1.0',
  'artifact_id': id,
  'session_id': sessionId,
  'thread_id': threadId,
  'epoch': 1,
  'task_id': taskId,
  'objective_revision': 1,
  'revision': revision,
  'title': 'Generated result $id',
  'summary': 'Synthetic result summary',
  'layout': 'stack',
  'components':
      components ??
      [
        artifactComponent('text', 'text', {
          'text': 'Synthetic generated content.',
        }),
      ],
  'created_at': 1793404800 + submitSequence,
  'updated_at': 1793404801 + submitSequence,
  'content_sha256': List.filled(64, 'a').join(),
  'content_scope': 'public_synthetic',
  'view_state': <String, Object?>{},
  'view_revision': 0,
  'task_submit_sequence': submitSequence,
};
