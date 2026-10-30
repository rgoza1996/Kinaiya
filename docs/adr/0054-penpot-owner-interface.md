# 0054: Implement the supplied Penpot owner interface

The owner explicitly requested implementation of the supplied native design
after #127. Issue #128 ports its Foundations, Converse, Agent, Options and narrow
screen compositions onto existing runtime owners. The visual reference contains
eleven pages and 43 boards; it does not supply new service capabilities.

## Shared implementation boundaries

`ui/design_tokens.dart` defines the nine named visual roles, rail, 12px radius,
locally bundled Inter family and the decorative moonlit-background asset path.
Use active Theme colors in panels and controls so explicit user overrides and
imported character palettes still win. The owner-requested Penpot navy/blue UI
is the new default interface presentation. Legacy character-pack brand metadata
and its #141091/#FFFFFF validation contract remain intact. No roadmap gate is
weakened by the presentation change.

Keep `SettingsHub({availability,buildInfo})` and SettingsAction unchanged. It
becomes a responsive Options page presented by the existing dialog route and
returns the chosen action only. Stable search/close/entry keys, availability,
Escape/focus/keyboard behavior and literal build labels remain. Additional
groups follow the supplied style; unsupported knobs are described as planned
instead of simulated. The current owners apply each setting independently.

CharacterView keeps its audio/session/epoch and mounted-view handles. Add an
optional `useDesignScene` flag (default false for standalone compatibility) that
HomeShell enables only for the default character/palette. MoonlitScene accepts
`useIllustration` (default false) for a local decorative background; custom
palettes and missing assets retain the deterministic painter. The raster
background excludes a painted character so the existing 32-frame semantic
mascot remains independently animated and honors reduced motion.

AgentChat keeps the same constructor, registry/draft-only controls, causal task
selection, approvals and current-state guards. Present existing real sessions,
tasks, controls and evidence in the supplied workspace columns. Do not create
the sample projects, task results, model names, harnesses or media items from the
drawing. Broader project/file/media APIs and companion owner pairing remain
separate work. Empty/offline/refused/uncertain states remain real states.

Root owns theme/font/art provenance, HomeShell and integration. Codex owns
CharacterView/MoonlitScene and Options modules in separate worktrees. A bounded
visible Muse Contributor xhigh worker owns Agent presentation against public
repository contracts; the native design archive is not forwarded to it. Preserve
genuine original output and failures before repairs. No backend, provider,
consent, permission or model experiment is part of the visual port.

## Verification

Retain behavioral regressions for audio, drafts, pane identity, task/approval
ownership and settings uncertainty. Review actual desktop and narrow builds,
320px/2x text and keyboard geometry, readable labels/focus and custom palettes.
Independent review and exact-head CI precede merge and preview replacement.
Compilation is not physical audio, mobile calling or latency acceptance.
