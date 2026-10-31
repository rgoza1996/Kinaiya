import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/api/agent_model_settings.dart';
import 'package:kinaiya_flutter/ui/options_model_editor.dart';

Map<String, Object?> modelsJson() => {
  'schema_version': '1.0',
  'revision': 3,
  'change_boundary': 'new_tasks',
  'default': {'profile_id': 'host-default', 'reasoning': null},
  'harnesses': {
    'seed-v1': {'profile_id': 'fast', 'reasoning': null},
  },
  'auxiliary': {
    'compression': {'profile_id': 'host-default', 'reasoning': null},
  },
  'harness_auxiliary': {
    'seed-v1': {
      'title_generation': {'profile_id': 'host-default', 'reasoning': null},
    },
  },
  'auxiliary_tasks': [
    {'id': 'analysis', 'label': 'Analysis'},
    {'id': 'compression', 'label': 'Context compression'},
    {'id': 'title_generation', 'label': 'Title generation'},
    {'id': 'delegation', 'label': 'Delegated reasoning'},
    {'id': 'harness_design', 'label': 'Harness design'},
  ],
  'profiles': [
    for (final id in ['host-default', 'fast'])
      {
        'id': id,
        'selectable': true,
        'auxiliary_selectable': true,
        'provider': {
          'provider': 'synthetic',
          'model': id == 'fast' ? 'Quick model' : 'Main model',
          'local': true,
        },
        'reasoning': null,
        'reasoning_options': ['low', 'high'],
      },
  ],
};

void main() {
  testWidgets('selected auxiliary purpose survives editor refresh', (
    tester,
  ) async {
    String? purpose;
    var mount = 0;
    Widget view() => MaterialApp(
      home: Scaffold(
        body: SingleChildScrollView(
          child: OptionsAuxiliaryModels(
            key: ValueKey(mount),
            models: AgentModelsSnapshot.fromJson(modelsJson()),
            initialPurpose: purpose,
            onPurposeChanged: (value) => purpose = value,
            busy: false,
            onSave: (_, _, _) async {},
          ),
        ),
      ),
    );
    await tester.pumpWidget(view());
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('auxiliary-task-analysis')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Context compression').last);
    await tester.pumpAndSettle();
    expect(purpose, 'compression');
    mount++;
    await tester.pumpWidget(view());
    await tester.pumpAndSettle();
    expect(
      find.byKey(const ValueKey('auxiliary-task-compression')),
      findsOneWidget,
    );
    expect(tester.takeException(), isNull);
  });

  test('auxiliary inheritance follows harness, task and Agent defaults', () {
    final models = AgentModelsSnapshot.fromJson(modelsJson());
    expect(models.effective(null, 'analysis').profileId, 'host-default');
    expect(models.effective('seed-v1', 'analysis').profileId, 'fast');
    expect(
      models.effective('seed-v1', 'compression').profileId,
      'host-default',
    );
    expect(
      models.effective('seed-v1', 'title_generation').profileId,
      'host-default',
    );
    expect(models.inherited('seed-v1', 'title_generation').profileId, 'fast');
    expect(models.overrides('seed-v1', 'title_generation'), isTrue);
    expect(models.overrides('seed-v1', 'analysis'), isFalse);
    for (final bad in [
      {
        ...modelsJson(),
        'auxiliary': {
          'unknown': {'profile_id': 'fast', 'reasoning': null},
        },
      },
      {
        ...modelsJson(),
        'auxiliary_tasks': [
          {'id': 'analysis', 'label': 'Analysis'},
          {'id': 'analysis', 'label': 'Duplicate'},
        ],
      },
    ]) {
      expect(() => AgentModelsSnapshot.fromJson(bad), throwsFormatException);
    }
    final legacy = modelsJson()
      ..remove('auxiliary_tasks')
      ..remove('auxiliary')
      ..remove('harness_auxiliary');
    expect(AgentModelsSnapshot.fromJson(legacy).auxiliaryTasks, isEmpty);
  });

  testWidgets(
    'auxiliary model choices save only the selected task on a gesture',
    (tester) async {
      final saved = <List<String?>>[];
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: SingleChildScrollView(
              child: OptionsAuxiliaryModels(
                models: AgentModelsSnapshot.fromJson(modelsJson()),
                busy: false,
                onSave: (purpose, profile, reasoning) async {
                  saved.add([purpose, profile, reasoning]);
                },
              ),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(saved, isEmpty);
      await tester.tap(find.text('Use a custom model'));
      await tester.pumpAndSettle();
      final picker = find.byKey(
        const ValueKey('harness-model-default-analysis-host-default-false'),
      );
      await tester.ensureVisible(picker);
      await tester.tap(picker);
      await tester.pumpAndSettle();
      await tester.tap(find.text('fast · Quick model').last);
      await tester.pumpAndSettle();
      final save = find.byKey(const ValueKey('auxiliary-model-save'));
      await tester.ensureVisible(save);
      await tester.tap(save);
      await tester.pumpAndSettle();
      expect(saved, [
        ['analysis', 'fast', null],
      ]);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'narrow enlarged harness auxiliary editor keeps usable scroll content',
    (tester) async {
      tester.view.physicalSize = const Size(320, 600);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      await tester.pumpWidget(
        MaterialApp(
          builder: (context, child) => MediaQuery(
            data: MediaQuery.of(
              context,
            ).copyWith(textScaler: TextScaler.linear(2)),
            child: child!,
          ),
          home: Scaffold(
            body: SingleChildScrollView(
              child: OptionsAuxiliaryModels(
                models: AgentModelsSnapshot.fromJson(modelsJson()),
                harness: 'seed-v1',
                busy: false,
                onSave: (_, __, ___) async {
                  fail('No automatic save');
                },
              ),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      await tester.ensureVisible(
        find.byKey(const ValueKey('auxiliary-model-save')),
      );
      await tester.pumpAndSettle();
      expect(find.text('Save auxiliary model').hitTestable(), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );
}
