import 'package:flutter/material.dart';


/// Skippable short book: a brief panel transition that opens Agent Chat.
/// Animation never delays speech; Skip jumps straight through.
class BookSheet extends StatefulWidget {
  const BookSheet({super.key, required this.onOpenAgent});
  final VoidCallback onOpenAgent;

  @override
  State<BookSheet> createState() => _BookSheetState();
}

class _BookSheetState extends State<BookSheet> {
  final _pages = PageController();
  int _index = 0;

  static const _panels = [
    (
      'Meet the worker',
      'Agent Chat does real tasks with visible tools, evidence and approvals.',
    ),
    (
      'You stay in control',
      'Steer or stop work any time. Nothing runs without your say-so.',
    ),
    (
      'Proof, not promises',
      'Success is shown only with evidence you can inspect.',
    ),
  ];

  @override
  void dispose() {
    _pages.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final reduced = MediaQuery.of(context).disableAnimations;
    return AlertDialog(
      title: const Text('A short note before Agent Chat'),
      content: SizedBox(
        width: 320,
        height: 180,
        child: PageView(
          controller: _pages,
          physics: reduced ? const NeverScrollableScrollPhysics() : null,
          onPageChanged: (i) => setState(() => _index = i),
          children: [
            for (final p in _panels)
              Semantics(
                label: '${p.$1}: ${p.$2}',
                child: Column(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    Text(p.$1, style: Theme.of(context).textTheme.titleMedium),
                    const SizedBox(height: 8),
                    Text(p.$2, textAlign: TextAlign.center),
                  ],
                ),
              ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () {
            Navigator.of(context).pop();
            widget.onOpenAgent();
          },
          child: const Text('Skip'),
        ),
        if (_index < _panels.length - 1)
          FilledButton(
            onPressed: () => _pages.nextPage(
              duration: reduced
                  ? Duration.zero
                  : const Duration(milliseconds: 200),
              curve: Curves.easeOut,
            ),
            child: const Text('Next'),
          )
        else
          FilledButton(
            onPressed: widget.onOpenAgent,
            child: const Text('Open Agent Chat'),
          ),
      ],
    );
  }
}
