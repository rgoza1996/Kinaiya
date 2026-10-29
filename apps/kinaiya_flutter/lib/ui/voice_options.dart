import 'package:flutter/material.dart';

/// Presentation-only Voice options sheet for Converse.
///
/// Displays the existing silence/timing controls without owning anything:
/// the silence value, the change handler, capture, drafts, sessions,
/// disclosure scopes, and audio all stay in [CharacterView]'s owner state.
/// Opening or closing this surface never starts, stops, rearms, or
/// otherwise touches capture; it only forwards an explicit silence change.
class VoiceOptionsSheet extends StatelessWidget {
  const VoiceOptionsSheet({
    super.key,
    required this.endSilenceMs,
    required this.silenceChoices,
    required this.onSilenceChanged,
    required this.windowMode,
  });

  final int endSilenceMs;
  final List<int> silenceChoices;
  final ValueChanged<int> onSilenceChanged;
  final bool windowMode;

  @override
  Widget build(BuildContext context) {
    return SafeArea(
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
                    style: Theme.of(context).textTheme.titleLarge,
                  ),
                ),
                IconButton(
                  tooltip: 'Close voice options',
                  icon: const Icon(Icons.close_rounded),
                  onPressed: () => Navigator.of(context).pop(),
                ),
              ],
            ),
            Flexible(
              child: SingleChildScrollView(
                child: Wrap(
                  spacing: 8,
                  runSpacing: 4,
                  crossAxisAlignment: WrapCrossAlignment.center,
                  children: [
                    const Text('Live'),
                    if (windowMode)
                      const Text('Conversation timing')
                    else ...[
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
                              child: Text('${(v / 1000).toStringAsFixed(1)}s'),
                            ),
                        ],
                        onChanged: (v) {
                          if (v == null) return;
                          onSilenceChanged(v);
                        },
                      ),
                    ],
                    const Text(
                      'Call for live listening · Tell for one message',
                      style: TextStyle(fontSize: 12),
                    ),
                  ],
                ),
              ),
            ),
          ],
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
    builder: (_) => StatefulBuilder(
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
  );
}
