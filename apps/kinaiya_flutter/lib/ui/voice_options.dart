import 'package:flutter/material.dart';

import '../audio/speech_audio.dart';
import 'design_tokens.dart';

/// Presentation-only Voice options sheet for Converse.
///
/// Displays microphone detection and timing controls without owning anything:
/// the settings, change handlers, capture, drafts, sessions,
/// disclosure scopes, and audio all stay in [CharacterView]'s owner state.
/// Opening or closing this surface never starts, stops, rearms, or
/// otherwise touches capture; it only forwards explicit setting changes.
///
/// Native audio takes display priority and shows buffered whole-utterance
/// end-of-speech controls. Modular streaming-window mode shows Decisions timing
/// and hides the fallback silence control; modular clip mode shows that control.
class VoiceOptionsSheet extends StatelessWidget {
  const VoiceOptionsSheet({
    super.key,
    required this.endSilenceMs,
    required this.silenceChoices,
    required this.onSilenceChanged,
    required this.windowMode,
    this.nativeAudio = false,
    this.embedded = false,
    this.vadSettings,
    this.onVadChanged,
    this.onRecalibrate,
    this.busy = false,
    this.notice,
    this.audioOutputs,
    this.audioOutputBusy = false,
    this.audioOutputNotice,
    this.onAudioOutputSelected,
    this.onChooseAudioOutput,
    this.onDefaultAudioOutput,
    this.onRefreshAudioOutputs,
    this.audioInputs,
    this.audioInputBusy = false,
    this.audioInputNotice,
    this.onAudioInputSelected,
    this.onDefaultAudioInput,
    this.onRefreshAudioInputs,
  });

  final int endSilenceMs;
  final List<int> silenceChoices;
  final ValueChanged<int> onSilenceChanged;
  final bool windowMode, nativeAudio, embedded;
  final VadSettings? vadSettings;
  final ValueChanged<VadSettings>? onVadChanged;
  final VoidCallback? onRecalibrate;
  final bool busy;
  final String? notice;
  final List<AudioOutputDevice>? audioOutputs;
  final bool audioOutputBusy;
  final String? audioOutputNotice;
  final ValueChanged<String>? onAudioOutputSelected;
  final VoidCallback? onChooseAudioOutput;
  final VoidCallback? onDefaultAudioOutput;
  final VoidCallback? onRefreshAudioOutputs;
  final List<AudioInputDevice>? audioInputs;
  final bool audioInputBusy;
  final String? audioInputNotice;
  final ValueChanged<String>? onAudioInputSelected;
  final VoidCallback? onDefaultAudioInput, onRefreshAudioInputs;

  Widget _inputOptions(BuildContext context) {
    final devices = audioInputs ?? const <AudioInputDevice>[];
    final selected = devices.where((device) => device.selected).toList();
    return Padding(
      padding: const EdgeInsets.only(bottom: KinaiyaDesign.spaceMd),
      child: DesignPanel(
        key: const ValueKey('voice-group-input'),
        padding: const EdgeInsets.all(KinaiyaDesign.spaceMd),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text('Audio input', style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 4),
            const Text(
              'Choose an input for the next Call or recording in this session. Refreshing devices does not activate a microphone. Permission and device labels are controlled by your browser.',
            ),
            if (devices.isNotEmpty) ...[
              const SizedBox(height: KinaiyaDesign.spaceSm),
              InputDecorator(
                key: const ValueKey('audio-input-device'),
                decoration: const InputDecoration(labelText: 'Available input'),
                child: DropdownButtonHideUnderline(
                  child: DropdownButton<String>(
                    value: selected.length == 1
                        ? selected.single.opaqueId
                        : null,
                    hint: const Text('Choose an input'),
                    isExpanded: true,
                    items: [
                      for (final device in devices)
                        DropdownMenuItem(
                          value: device.opaqueId,
                          child: Text(
                            device.label,
                            overflow: TextOverflow.ellipsis,
                          ),
                        ),
                    ],
                    onChanged: audioInputBusy || onAudioInputSelected == null
                        ? null
                        : (id) {
                            if (id != null) onAudioInputSelected!(id);
                          },
                  ),
                ),
              ),
            ],
            const SizedBox(height: KinaiyaDesign.spaceSm),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                OutlinedButton(
                  key: const ValueKey('audio-input-default'),
                  onPressed: audioInputBusy ? null : onDefaultAudioInput,
                  child: const Text('Use browser default input'),
                ),
                TextButton(
                  key: const ValueKey('audio-input-refresh'),
                  onPressed: audioInputBusy ? null : onRefreshAudioInputs,
                  child: const Text('Refresh inputs'),
                ),
              ],
            ),
            if (audioInputBusy) const LinearProgressIndicator(),
            if (audioInputNotice != null)
              Semantics(
                liveRegion: true,
                child: Text(
                  audioInputNotice!,
                  key: const ValueKey('audio-input-notice'),
                ),
              ),
          ],
        ),
      ),
    );
  }

  Widget _outputOptions(BuildContext context) {
    final devices = audioOutputs ?? const <AudioOutputDevice>[];
    final selected = devices.where((device) => device.selected).toList();
    final selectedId = selected.length == 1 ? selected.single.opaqueId : null;
    return Padding(
      padding: const EdgeInsets.only(bottom: KinaiyaDesign.spaceMd),
      child: DesignPanel(
        key: const ValueKey('voice-group-output'),
        padding: const EdgeInsets.all(KinaiyaDesign.spaceMd),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              'Audio output',
              style: Theme.of(context).textTheme.titleMedium,
            ),
            const SizedBox(height: 4),
            const Text(
              'Choose where this session plays speech. This does not change your microphone or system default.',
            ),
            const SizedBox(height: KinaiyaDesign.spaceSm),
            if (devices.isNotEmpty)
              InputDecorator(
                key: const ValueKey('audio-output-device'),
                decoration: const InputDecoration(
                  labelText: 'Available output',
                  helperText: 'Device permission and availability are controlled by your browser.',
                  helperMaxLines: 4,
                ),
                child: DropdownButtonHideUnderline(
                  child: DropdownButton<String>(
                    value: selectedId,
                    hint: const Text('Choose an output'),
                    isExpanded: true,
                    items: [
                      for (final device in devices)
                        DropdownMenuItem(
                          value: device.opaqueId,
                          child: Text(
                            device.label,
                            overflow: TextOverflow.ellipsis,
                          ),
                        ),
                    ],
                    onChanged: audioOutputBusy || onAudioOutputSelected == null
                        ? null
                        : (id) {
                            if (id != null) onAudioOutputSelected!(id);
                          },
                  ),
                ),
              ),
            const SizedBox(height: KinaiyaDesign.spaceSm),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                OutlinedButton(
                  key: const ValueKey('audio-output-choose'),
                  onPressed: audioOutputBusy ? null : onChooseAudioOutput,
                  child: const Text('Choose output'),
                ),
                OutlinedButton(
                  key: const ValueKey('audio-output-default'),
                  onPressed: audioOutputBusy ? null : onDefaultAudioOutput,
                  child: const Text('Use system default'),
                ),
                TextButton(
                  key: const ValueKey('audio-output-refresh'),
                  onPressed: audioOutputBusy ? null : onRefreshAudioOutputs,
                  child: const Text('Refresh devices'),
                ),
              ],
            ),
            if (audioOutputBusy)
              const LinearProgressIndicator(
                semanticsLabel: 'Selecting audio output',
              ),
            if (audioOutputNotice != null || audioOutputs == null) ...[
              const SizedBox(height: KinaiyaDesign.spaceSm),
              Semantics(
                liveRegion: true,
                child: Text(
                  audioOutputNotice ??
                      'Audio output selection is unavailable in this browser.',
                  key: const ValueKey('audio-output-notice'),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }

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
    final textTheme = Theme.of(context).textTheme;
    final scheme = Theme.of(context).colorScheme;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        DesignPanel(
          key: const ValueKey('voice-group-detection'),
          padding: const EdgeInsets.all(KinaiyaDesign.spaceMd),
          child: Material(
            type: MaterialType.transparency,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(
                  'Microphone detection',
                  style: textTheme.titleMedium?.copyWith(
                    fontWeight: FontWeight.w700,
                  ),
                ),
                const SizedBox(height: 4),
                Text(
                  'Local microphone settings for this session.',
                  style: textTheme.bodySmall?.copyWith(
                    color: scheme.onSurfaceVariant,
                  ),
                ),
                const SizedBox(height: KinaiyaDesign.spaceSm),
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
                const SizedBox(height: KinaiyaDesign.spaceSm),
                DropdownButtonFormField<VadSensitivity>(
                  key: ValueKey('vad-sensitivity-${settings.sensitivity.name}'),
                  initialValue: settings.sensitivity,
                  isExpanded: true,
                  decoration: const InputDecoration(
                    labelText: 'Speech sensitivity',
                    helperText: 'Higher sensitivity picks up softer speech and more background sound.',
                    helperMaxLines: 4,
                  ),
                  items: [
                    for (final value in VadSensitivity.values)
                      DropdownMenuItem(
                        value: value,
                        child: Text(
                          value.label,
                          overflow: TextOverflow.ellipsis,
                        ),
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
                if (busy) ...[
                  const SizedBox(height: KinaiyaDesign.spaceSm),
                  const LinearProgressIndicator(
                    semanticsLabel: 'Applying microphone settings',
                  ),
                ],
                if (notice != null) ...[
                  const SizedBox(height: KinaiyaDesign.spaceSm),
                  Semantics(
                    liveRegion: true,
                    child: Text(
                      notice!,
                      key: const ValueKey('voice-group-status-notice'),
                      style: textTheme.bodySmall?.copyWith(
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                ],
              ],
            ),
          ),
        ),
        const SizedBox(height: KinaiyaDesign.spaceMd),
        DesignPanel(
          key: const ValueKey('voice-group-calibration'),
          padding: const EdgeInsets.all(KinaiyaDesign.spaceMd),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                'Room noise calibration & confirmation',
                style: textTheme.titleSmall?.copyWith(
                  fontWeight: FontWeight.w700,
                ),
              ),
              const SizedBox(height: 4),
              Text(
                'With Call on, remain quiet for about one second. Otherwise calibration waits for your next call.',
                style: textTheme.bodySmall?.copyWith(
                  color: scheme.onSurfaceVariant,
                ),
              ),
              const SizedBox(height: KinaiyaDesign.spaceSm),
              Align(
                alignment: Alignment.centerLeft,
                child: OutlinedButton.icon(
                  key: const Key('vad-recalibrate'),
                  onPressed: busy || !settings.adaptive ? null : onRecalibrate,
                  icon: const Icon(Icons.graphic_eq),
                  label: const Text('Recalibrate room noise'),
                ),
              ),
              if (nativeAudio || !windowMode) ...[
                const SizedBox(height: KinaiyaDesign.spaceMd),
                _duration(
                  label: 'Speech confirmation',
                  help: 'Ignore shorter sounds before confirming speech in clip mode.',
                  value: settings.minSpeechMs,
                  values: const [100, 200, 300, 500, 1000, 2000],
                  onChanged: change == null
                      ? null
                      : (value) =>
                            change(settings.copyWith(minSpeechMs: value)),
                ),
                _duration(
                  label: 'Interrupt confirmation',
                  help: 'Sustained speech needed to stop Kinaiya’s playback in clip mode.',
                  value: settings.bargeMs,
                  values: const [150, 220, 350, 500, 1000],
                  onChanged: change == null
                      ? null
                      : (value) => change(settings.copyWith(bargeMs: value)),
                ),
              ],
            ],
          ),
        ),
        const SizedBox(height: KinaiyaDesign.spaceMd),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    final textTheme = Theme.of(context).textTheme;
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
                          ? textTheme.titleMedium
                          : textTheme.titleLarge,
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
                      if (audioInputs != null ||
                          audioInputNotice != null ||
                          onDefaultAudioInput != null ||
                          onRefreshAudioInputs != null)
                        _inputOptions(context),
                      if (audioOutputs != null ||
                          audioOutputNotice != null ||
                          onChooseAudioOutput != null ||
                          onDefaultAudioOutput != null ||
                          onRefreshAudioOutputs != null)
                        _outputOptions(context),
                      if (vadSettings case final settings?)
                        _dynamicOptions(context, settings),
                      DesignPanel(
                        key: const ValueKey('voice-group-timing'),
                        padding: const EdgeInsets.all(KinaiyaDesign.spaceMd),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              'Turn & clip timing',
                              key: const ValueKey('voice-group-timing-heading'),
                              style: textTheme.titleSmall?.copyWith(
                                fontWeight: FontWeight.w700,
                              ),
                            ),
                            const SizedBox(height: KinaiyaDesign.spaceSm),
                            Wrap(
                              spacing: 8,
                              runSpacing: 4,
                              crossAxisAlignment: WrapCrossAlignment.center,
                              children: [
                                Text(
                                  nativeAudio
                                      ? 'Buffered native audio'
                                      : windowMode
                                      ? 'Live'
                                      : 'Clip capture fallback',
                                ),
                                if (windowMode && !nativeAudio) ...[
                                  const Text('Conversation timing'),
                                  const Text(
                                    'Live audio streams continuously. Decisions chooses when to wait, respond, or interrupt; no silence timer ends the turn.',
                                    style: TextStyle(fontSize: 12),
                                  ),
                                ] else ...[
                                  Tooltip(
                                    message: nativeAudio
                                        ? 'End-of-speech detection for a buffered native utterance'
                                        : 'Silence that ends a voice clip',
                                    child: const Text('Silence'),
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
                                  Text(
                                    nativeAudio
                                        ? 'Buffered native whole-utterance capture: end-of-speech detection finalizes each utterance, up to 30 seconds.'
                                        : 'Fallback clip capture: silence ends the clip. Not live model timing.',
                                    style: const TextStyle(fontSize: 12),
                                  ),
                                ],
                                Text(
                                  nativeAudio
                                      ? 'Call captures whole utterances · Tell sends one message'
                                      : 'Call for live listening · Tell for one message',
                                  style: const TextStyle(fontSize: 12),
                                ),
                              ],
                            ),
                          ],
                        ),
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
