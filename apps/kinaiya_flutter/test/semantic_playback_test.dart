import 'package:flutter_test/flutter_test.dart';
import 'package:kinaiya_flutter/character/pack.dart';
import 'package:kinaiya_flutter/character/playback.dart';

CharacterPack playbackPack({
  int schema = 3,
  String id = 'public-semantic-fixture',
  String version = '1.0',
  Map<String, List<PackVariant>> activities = const {},
  Map<String, String> fallbacks = const {},
  Set<String> oneShots = const {},
}) => CharacterPack(
  version: schema,
  name: 'Synthetic',
  fallbackAsset: 'assets/source.png',
  poses: const [],
  fallback: false,
  packId: id,
  packVersion: version,
  animations: {
    for (final name in [
      'idle',
      'listening',
      'speaking',
      'thinking',
      'inspect',
      'code',
      'email',
      'warm',
      'critical',
    ])
      name: PackAnimation(
        sheet: 'core',
        frames: [0, 1, 2, 3],
        frameMs: 100,
        loop: !oneShots.contains(name),
      ),
  },
  activities: activities,
  fallbacks: fallbacks,
);

CharacterSignals work(String activity) => CharacterSignals(activity: activity);
Duration ms(int value) => Duration(milliseconds: value);

void main() {
  test(
    'signal priority and conditions use confirmed flags and bounded semantics',
    () {
      expect(
        const CharacterSignals(
          speaking: true,
          listening: true,
          warming: true,
          blocked: true,
          activity: 'alert.critical',
        ).semantic,
        'speaking',
      );
      expect(
        const CharacterSignals(
          listening: true,
          warming: true,
          activity: 'alert.critical',
        ).semantic,
        'listening',
      );
      expect(
        const CharacterSignals(
          warming: true,
          activity: 'alert.critical',
        ).semantic,
        'alert.critical',
      );
      expect(
        const CharacterSignals(
          warming: true,
          blocked: true,
          activity: 'working.write_code',
        ).semantic,
        'warming',
      );
      expect(
        const CharacterSignals(
          blocked: true,
          activity: 'working.write_code',
        ).semantic,
        'blocked',
      );
      expect(const CharacterSignals(working: true).semantic, 'working.generic');
      expect(
        const CharacterSignals(working: true, activity: 'unknown').semantic,
        'idle',
      );
      expect(
        const CharacterSignals(activity: 'working.unknown').semantic,
        'idle',
      );
      expect(
        const CharacterSignals(activity: 'working.reason').semantic,
        'working.reason',
      );
      final conditions = const CharacterSignals(
        listening: true,
        warming: true,
        blocked: true,
        activity: 'working.write_code',
      ).conditions;
      expect(conditions, {'listening', 'warming', 'blocked', 'working'});
      expect(const CharacterSignals().conditions, {'quiet'});
      expect(() => conditions.clear(), throwsUnsupportedError);
    },
  );

  test('schema2 resolves old animation IDs and reason alias compatibly', () {
    final playback = CharacterPlayback(playbackPack(schema: 2));
    expect(
      playback.update(const CharacterSignals(), ms(0)).animationId,
      'idle',
    );
    expect(
      playback
          .update(const CharacterSignals(listening: true), ms(1))
          .animationId,
      'listening',
    );
    expect(
      playback.update(work('working.reason'), ms(2)).animationId,
      'thinking',
    );
    expect(
      playback.update(work('working.write_code'), ms(700)).animationId,
      'idle',
    );
    expect(
      playback
          .update(const CharacterSignals(speaking: true), ms(701))
          .animationId,
      'speaking',
    );
  });

  test('work and warmup standard fallbacks keep requested semantic honest', () {
    final playback = CharacterPlayback(playbackPack());
    for (final activity in [
      'working.inspect_repo',
      'working.generic',
      'working.reason',
    ]) {
      final frame = playback.update(
        work(activity),
        ms(1000 * (characterSemanticIds.toList().indexOf(activity) + 1)),
      );
      expect(frame.animationId, 'thinking');
      expect(frame.semantic, activity);
    }
    final warming = playback.update(
      const CharacterSignals(warming: true),
      ms(20000),
    );
    expect(warming.animationId, 'idle');
    expect(warming.semantic, 'warming');
    expect(
      playback
          .update(
            const CharacterSignals(activity: 'alert.important'),
            ms(20001),
          )
          .animationId,
      'idle',
    );
  });

  test(
    'explicit fallback precedes standard and known unmapped target works',
    () {
      final playback = CharacterPlayback(
        playbackPack(
          activities: {
            'working.generic': [PackVariant(animation: 'inspect')],
            'blocked': [PackVariant(animation: 'critical')],
          },
          fallbacks: {'working.write_code': 'blocked', 'warming': 'speaking'},
        ),
      );
      expect(
        playback.update(work('working.write_code'), ms(0)).animationId,
        'critical',
      );
      expect(
        playback
            .update(const CharacterSignals(warming: true), ms(1))
            .animationId,
        'speaking',
      );
      expect(
        playback.update(work('working.inspect_repo'), ms(2)).animationId,
        'inspect',
      );
    },
  );

  test('explicit fallback also precedes an unmapped core default', () {
    final playback = CharacterPlayback(
      playbackPack(fallbacks: {'idle': 'listening'}),
    );
    final frame = playback.update(const CharacterSignals(), ms(0));
    expect(frame.semantic, 'idle');
    expect(frame.animationId, 'listening');
  });

  test(
    'explicit/standard fallback interaction terminates without blank frames',
    () {
      final playback = CharacterPlayback(
        playbackPack(
          activities: {
            'idle': [
              PackVariant(animation: 'inspect', when: {'speaking'}),
            ],
          },
          fallbacks: {'idle': 'blocked'},
        ),
      );
      final frame = playback.update(const CharacterSignals(), ms(0));
      expect(frame.animationId, 'idle');
      expect(frame.semantic, 'idle');
    },
  );

  test(
    'weighted selection is deterministic, stable within cycles and proportional',
    () {
      final pack = playbackPack(
        activities: {
          'idle': [
            PackVariant(animation: 'inspect', weight: 1),
            PackVariant(animation: 'code', weight: 3),
          ],
        },
      );
      final first = CharacterPlayback(pack), second = CharacterPlayback(pack);
      final counts = <String, int>{'inspect': 0, 'code': 0};
      for (var cycle = 0; cycle < 2000; cycle++) {
        final time = cycle * 400;
        final a = first.update(const CharacterSignals(), ms(time));
        final b = second.update(const CharacterSignals(), ms(time));
        expect(a.animationId, b.animationId);
        expect(a.animationElapsed, Duration.zero);
        counts[a.animationId] = counts[a.animationId]! + 1;
        final mid = first.update(const CharacterSignals(), ms(time + 399));
        expect(mid.animationId, a.animationId);
        expect(mid.animationElapsed, ms(399));
      }
      expect(counts['inspect'], inInclusiveRange(450, 550));
      expect(counts['code'], inInclusiveRange(1450, 1550));
    },
  );

  test(
    'pack identity/version participate in deterministic variant sequence',
    () {
      Map<String, List<PackVariant>> variants() => {
        'idle': [
          PackVariant(animation: 'inspect', weight: 31),
          PackVariant(animation: 'code', weight: 69),
        ],
      };
      List<String> sequence(CharacterPack pack) {
        final playback = CharacterPlayback(pack);
        return [
          for (var i = 0; i < 40; i++)
            playback.update(const CharacterSignals(), ms(i * 400)).animationId,
        ];
      }

      final baseline = sequence(playbackPack(activities: variants()));
      expect(
        sequence(playbackPack(id: 'different', activities: variants())),
        isNot(baseline),
      );
      expect(
        sequence(playbackPack(version: 'different', activities: variants())),
        isNot(baseline),
      );
    },
  );

  test(
    'cooldown starts on selection, holds cycle, falls back and expires exactly',
    () {
      final playback = CharacterPlayback(
        playbackPack(
          activities: {
            'working.write_code': [
              PackVariant(animation: 'code', cooldownMs: 1000),
            ],
          },
        ),
      );
      final signals = work('working.write_code');
      expect(playback.update(signals, ms(0)).animationId, 'code');
      expect(playback.update(signals, ms(399)).animationId, 'code');
      expect(playback.update(signals, ms(400)).animationId, 'thinking');
      expect(
        playback.update(const CharacterSignals(), ms(999)).animationId,
        'idle',
      );
      expect(playback.update(signals, ms(1000)).animationId, 'code');
      expect(playback.update(signals, ms(1400)).animationId, 'thinking');
    },
  );

  test(
    'all AND conditions apply and false conditions release a variant immediately',
    () {
      final playback = CharacterPlayback(
        playbackPack(
          activities: {
            'idle': [
              PackVariant(animation: 'inspect', when: {'quiet', 'working'}),
            ],
          },
        ),
      );
      expect(
        playback
            .update(
              const CharacterSignals(working: true, activity: 'unknown'),
              ms(0),
            )
            .animationId,
        'inspect',
      );
      expect(
        playback.update(const CharacterSignals(), ms(1)).animationId,
        'idle',
      );
      // Quiet is false while listening; selection is the real listening semantic.
      expect(
        playback
            .update(
              const CharacterSignals(listening: true, working: true),
              ms(2),
            )
            .animationId,
        'listening',
      );
    },
  );

  test(
    'work hysteresis lasts650ms and retains honest newly requested semantic',
    () {
      final playback = CharacterPlayback(
        playbackPack(
          activities: {
            'working.inspect_repo': [PackVariant(animation: 'inspect')],
            'working.write_code': [PackVariant(animation: 'code')],
          },
        ),
      );
      expect(
        playback.update(work('working.inspect_repo'), ms(0)).animationId,
        'inspect',
      );
      final held = playback.update(work('working.write_code'), ms(100));
      expect(held.animationId, 'inspect');
      expect(held.semantic, 'working.write_code');
      expect(
        playback.update(work('working.write_code'), ms(400)).animationId,
        'inspect',
      );
      expect(
        playback.update(work('working.write_code'), ms(649)).animationId,
        'inspect',
      );
      expect(
        playback.update(work('working.write_code'), ms(650)).animationId,
        'code',
      );
      expect(
        playback.update(work('working.inspect_repo'), ms(700)).animationId,
        'code',
      );
      expect(
        playback.update(work('working.inspect_repo'), ms(1300)).animationId,
        'inspect',
      );
    },
  );

  test(
    'urgent/readiness transitions bypass work hold and do not wait for approach',
    () {
      for (final signals in [
        const CharacterSignals(speaking: true, warming: true),
        const CharacterSignals(listening: true),
        const CharacterSignals(activity: 'alert.critical'),
        const CharacterSignals(blocked: true),
        const CharacterSignals(),
      ]) {
        final playback = CharacterPlayback(
          playbackPack(
            activities: {
              'working.write_code': [PackVariant(animation: 'code')],
              'alert.critical': [PackVariant(animation: 'critical')],
            },
          ),
        );
        playback.update(work('working.write_code'), ms(0));
        final frame = playback.update(signals, ms(1));
        expect(frame.semantic, signals.semantic);
        expect(frame.animationId, isNot('code'));
        expect(frame.approach, 1);
      }
    },
  );

  test(
    'warming enters0 reaches1 over1200ms and ready speech exits immediately',
    () {
      final playback = CharacterPlayback(
        playbackPack(
          activities: {
            'warming': [PackVariant(animation: 'warm')],
          },
        ),
      );
      expect(
        playback
            .update(const CharacterSignals(warming: true), ms(100))
            .approach,
        0,
      );
      expect(
        playback
            .update(const CharacterSignals(warming: true), ms(700))
            .approach,
        .5,
      );
      expect(
        playback
            .update(const CharacterSignals(warming: true), ms(1300))
            .approach,
        1,
      );
      expect(
        playback
            .update(const CharacterSignals(warming: true), ms(5000))
            .approach,
        1,
      );
      final speech = playback.update(
        const CharacterSignals(speaking: true, warming: true),
        ms(5001),
      );
      expect(speech.animationId, 'speaking');
      expect(speech.approach, 1);
      expect(speech.animationElapsed, Duration.zero);
      expect(
        playback
            .update(const CharacterSignals(warming: true), ms(5002))
            .approach,
        0,
      );
      expect(playback.update(const CharacterSignals(), ms(5003)).approach, 1);
    },
  );

  test(
    'regressing and negative clocks clamp without backward animation or approach',
    () {
      final playback = CharacterPlayback(playbackPack());
      final first = playback.update(
        const CharacterSignals(warming: true),
        ms(-10),
      );
      expect(first.animationElapsed, Duration.zero);
      expect(first.approach, 0);
      final later = playback.update(
        const CharacterSignals(warming: true),
        ms(300),
      );
      final regression = playback.update(
        const CharacterSignals(warming: true),
        ms(200),
      );
      expect(regression.animationElapsed, later.animationElapsed);
      expect(regression.approach, later.approach);
    },
  );

  test(
    'one-shots clamp without restart or variant churn absent semantic change',
    () {
      final playback = CharacterPlayback(
        playbackPack(
          activities: {
            'idle': [PackVariant(animation: 'inspect', cooldownMs: 1000)],
          },
          oneShots: {'inspect'},
        ),
      );
      playback.update(const CharacterSignals(), ms(0));
      final completed = playback.update(const CharacterSignals(), ms(10000));
      expect(completed.animationId, 'inspect');
      expect(completed.animationElapsed, ms(10000));
      expect(
        CharacterPack.frameIndexFor(
          playback.pack.animations['inspect']!,
          completed.animationElapsed,
        ),
        3,
      );
      playback.restartAnimation(ms(10000));
      expect(
        playback.update(const CharacterSignals(), ms(10000)).animationElapsed,
        ms(10000),
      );
      playback.update(const CharacterSignals(speaking: true), ms(10001));
      expect(
        playback.update(const CharacterSignals(), ms(10002)).animationElapsed,
        Duration.zero,
      );
    },
  );

  test('phase-only restart preserves variant cooldown and cycle selection', () {
    final playback = CharacterPlayback(
      playbackPack(
        activities: {
          'idle': [PackVariant(animation: 'inspect', cooldownMs: 1000)],
        },
      ),
    );
    playback.update(const CharacterSignals(), ms(0));
    expect(
      playback.update(const CharacterSignals(), ms(350)).animationElapsed,
      ms(350),
    );
    playback.restartAnimation(ms(350));
    expect(
      playback.update(const CharacterSignals(), ms(350)).animationElapsed,
      Duration.zero,
    );
    expect(
      playback.update(const CharacterSignals(), ms(749)).animationId,
      'inspect',
    );
    expect(
      playback.update(const CharacterSignals(), ms(750)).animationId,
      'idle',
    );
    playback.update(const CharacterSignals(speaking: true), ms(999));
    // Cooldown was selected at0, rather than being restarted at350.
    expect(
      playback.update(const CharacterSignals(), ms(1000)).animationId,
      'inspect',
    );
  });

  test('large clock jumps select once without replaying skipped cycles', () {
    final playback = CharacterPlayback(
      playbackPack(
        activities: {
          'idle': [PackVariant(animation: 'inspect', cooldownMs: 1000)],
        },
      ),
    );
    playback.update(const CharacterSignals(), ms(0));
    final afterGap = playback.update(const CharacterSignals(), ms(100000));
    expect(afterGap.animationId, 'inspect');
    expect(afterGap.animationElapsed, Duration.zero);
    expect(
      playback.update(const CharacterSignals(), ms(100400)).animationId,
      'idle',
    );
  });
}
