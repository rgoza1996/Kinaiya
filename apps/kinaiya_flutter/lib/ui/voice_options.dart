import 'package:flutter/material.dart';

import '../audio/vad_settings.dart';

/// Presentation-only Voice options sheet for Converse.
///
/// Displays microphone detection and timing controls without owning anything:
/// the settings, change handlers, capture, drafts, sessions,
/// disclosure scopes, and audio all stay in [CharacterView]'s owner state.
/// Opening or closing this surface never starts, stops, rearms, or
/// otherwise touches capture; it only forwards explicit setting changes.
///
/// In streaming-window mode the sheet shows live Decisions timing and hides
/// the fallback silence control. Otherwise it shows the fallback clip
/// silence control.
class VoiceOptionsSheet extends StatelessWidget {
  const VoiceOptionsSheet({
    super.key,
    required this.endSilenceMs,
    required this.silenceChoices,
    required this.onSilenceChanged,
    required this.windowMode,
    this.embedded = false,
    this.vadSettings,
    this.onVadChanged,
    this.onRecalibrate,
    this.busy = false,
    this.notice,
  });

  final int endSilenceMs;
  final List<int> silenceChoices;
  final ValueChanged<int> onSilenceChanged;
  final bool windowMode, embedded;
  final VadSettings? vadSettings;
  final ValueChanged<VadSettings>? onVadChanged;
  final VoidCallback? onRecalibrate;
  final bool busy;
  final String? notice;

  Widget _duration({
    required String label,
    required String help,
    required int value,
    required List<int> values,
    required ValueChanged<int>? onChanged,
  }) => Padding(
    padding: const EdgeInsets.only(bottom: 20),
    child: DropdownButtonFormField<String>(
      key: ValueKey('$label-$value'),
      initialValue: '$value',
      isExpanded: true,
      decoration: InputDecoration(
        labelText: label,
        helperText: help,
        helperMaxLines: 4,
      ),
      items: [
        for (final item in {value, ...values}.toList()..sort())
          DropdownMenuItem(value: '$item', child: Text('$item ms')),
      ],
      onChanged: onChanged == null
          ? null
          : (next) {
              if (next != null) onChanged(int.parse(next));
            },
    ),
  );

  Widget _dynamicOptions(BuildContext context, VadSettings settings) {
    final change = busy ? null : onVadChanged;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text(
          'Microphone detection',
          style: Theme.of(context).textTheme.titleMedium,
        ),
        const SizedBox(height: 8),
        const Text('Local microphone settings for this session.'),
        SwitchListTile.adaptive(
          key: const Key('vad-adaptive'),
          contentPadding: EdgeInsets.zero,
          title: const Text('Dynamic VAD'),
          subtitle: Text(
            settings.adaptive
                ? 'Calibrate to the room when listening starts and adapt to quiet background noise.'
                : 'Fixed detection thresholds. Sensitivity still applies.',
          ),
          value: settings.adaptive,
          onChanged: change == null
              ? null
              : (value) => change(settings.copyWith(adaptive: value)),
        ),
        const SizedBox(height: 12),
        DropdownButtonFormField<VadSensitivity>(
          key: ValueKey('vad-sensitivity-${settings.sensitivity.name}'),
          initialValue: settings.sensitivity,
          isExpanded: true,
          decoration: const InputDecoration(
            labelText: 'Speech sensitivity',
            helperText:
                'Higher sensitivity picks up softer speech and more background sound.',
            helperMaxLines: 4,
          ),
          items: [
            for (final value in VadSensitivity.values)
              DropdownMenuItem(
                value: value,
                child: Text(value.label, overflow: TextOverflow.ellipsis),
              ),
          ],
          onChanged: change == null
              ? null
              : (value) {
                  if (value != null) {
                    change(settings.copyWith(sensitivity: value));
                  }
                },
        ),
        const SizedBox(height: 20),
        Align(
          alignment: Alignment.centerLeft,
          child: OutlinedButton.icon(
            key: const Key('vad-recalibrate'),
            onPressed: busy || !settings.adaptive ? null : onRecalibrate,
            icon: const Icon(Icons.graphic_eq),
            label: const Text('Recalibrate room noise'),
          ),
        ),
        const SizedBox(height: 8),
        const Text(
          'With Call on, remain quiet for about one second. Otherwise calibration waits for your next call.',
        ),
        const SizedBox(height: 24),
        if (!windowMode) ...[
          _duration(
            label: 'Speech confirmation',
            help:
                'Ignore shorter sounds before confirming speech in clip mode.',
            value: settings.minSpeechMs,
            values: const [100, 200, 300, 500, 1000, 2000],
            onChanged: change == null
                ? null
                : (value) => change(settings.copyWith(minSpeechMs: value)),
          ),
          _duration(
            label: 'Interrupt confirmation',
            help:
                'Sustained speech needed to stop Kinaiya’s playback in clip mode.',
            value: settings.bargeMs,
            values: const [150, 220, 350, 500, 1000],
            onChanged: change == null
                ? null
                : (value) => change(settings.copyWith(bargeMs: value)),
          ),
        ],
        if (busy)
          const LinearProgressIndicator(
            semanticsLabel: 'Applying microphone settings',
          ),
        if (notice != null)
          Padding(
            padding: const EdgeInsets.only(bottom: 16),
            child: Semantics(liveRegion: true, child: Text(notice!)),
          ),
        const Divider(),
        const SizedBox(height: 12),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(
      builder: (context, bounds) => SafeArea(
        top: false,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 20),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(
                      'Voice options',
                      maxLines: bounds.maxHeight < 240 ? 1 : null,
                      overflow: TextOverflow.ellipsis,
                      style: bounds.maxHeight < 240
                          ? Theme.of(context).textTheme.titleMedium
                          : Theme.of(context).textTheme.titleLarge,
                    ),
                  ),
                  if (!embedded)
                    IconButton(
                      tooltip: 'Close voice options',
                      icon: const Icon(Icons.close_rounded),
                      onPressed: () => Navigator.of(context).pop(),
                    ),
                ],
              ),
              Flexible(
                child: SingleChildScrollView(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      if (vadSettings case final settings?)
                        _dynamicOptions(context, settings),
                      Wrap(
                        spacing: 8,
                        runSpacing: 4,
                        crossAxisAlignment: WrapCrossAlignment.center,
                        children: [
                          Text(windowMode ? 'Live' : 'Clip capture fallback'),
                          if (windowMode) ...[
                            const Text('Conversation timing'),
                            const Text(
                              'Live audio streams continuously. Decisions chooses when to wait, respond, or interrupt; no silence timer ends the turn.',
                              style: TextStyle(fontSize: 12),
                            ),
                          ] else ...[
                            const Tooltip(
                              message: 'Silence that ends a voice clip',
                              child: Text('Silence'),
                            ),
                            DropdownButton<int>(
                              value: endSilenceMs,
                              isDense: true,
                              items: [
                                for (final v in silenceChoices)
                                  DropdownMenuItem(
                                    value: v,
                                    child: Text(
                                      '${(v / 1000).toStringAsFixed(1)}s',
                                    ),
                                  ),
                              ],
                              onChanged: busy
                                  ? null
                                  : (v) {
                                      if (v == null) return;
                                      onSilenceChanged(v);
                                    },
                            ),
                            const Text(
                              'Fallback clip capture: silence ends the clip. Not live model timing.',
                              style: TextStyle(fontSize: 12),
                            ),
                          ],
                          const Text(
                            'Call for live listening · Tell for one message',
                            style: TextStyle(fontSize: 12),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// Opens [VoiceOptionsSheet] as an accessible modal bottom sheet.
///
/// Presentation only: dismissing it leaves capture, drafts, sessions, and
/// audio untouched. The silence handler runs only on an explicit change.
Future<void> showVoiceOptions({
  required BuildContext context,
  required int endSilenceMs,
  required List<int> silenceChoices,
  required ValueChanged<int> onSilenceChanged,
  required bool windowMode,
}) {
  var selection = endSilenceMs;
  return showModalBottomSheet<void>(
    context: context,
    showDragHandle: true,
    isScrollControlled: true,
    builder: (sheetContext) => Padding(
      padding: EdgeInsets.only(
        bottom: MediaQuery.viewInsetsOf(sheetContext).bottom,
      ),
      child: StatefulBuilder(
        builder: (_, updateDisplay) => VoiceOptionsSheet(
          endSilenceMs: selection,
          silenceChoices: silenceChoices,
          onSilenceChanged: (value) {
            updateDisplay(() => selection = value);
            onSilenceChanged(value);
          },
          windowMode: windowMode,
        ),
      ),
    ),
  );
}
