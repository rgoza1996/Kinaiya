# Native work and optional companion design synchronization

The repository's canonical `assets/penpot/Kinaiya-Converse-Agent.penpot` now
contains a new **13 · Native work & companions** page. This is a local export
extension of the existing master, not a change to an online Penpot workspace.
It supports the issue #476–#587 work while preserving the earlier designs.

Source: 244,662,707 bytes, SHA-256
`1fa59798c57172b75afb527a671a44dc44e9c9509d7b4a496030b13f2683b59c`.
Result: 245,700,930 bytes, SHA-256
`039ff39d6b2c41933a4b457e3ecbe028f1cb8fef572c40797c1d5ffea49e3ef7`.
Master file ID remains `aed0c863-4a2a-8195-8008-defdd82e75a3`.
New page ID: `3d154fd3-c631-5f34-bf0e-4efa76283a20`.

## Requirement and design mapping

| Owner requirement / issues | New design behavior | Qualification boundary |
| --- | --- | --- |
| Native handoff and status: #483, #485–#486, #559 | Seven task states distinguish generated receipt, native acceptance, pending steer, failed partial work, UI publication, execution receipt, received outcome and unavailable observations | Illustrative public dialogue; no new model-quality pass or execution fact |
| Editable model preferences: #557–#559 | Separate Converse and Decisions natural-language instruction concepts; configurable handoff preference without mandatory silence, permissions or success authority | Converse host configuration exists in draft #589. GUI editor and Decisions configuration are proposed |
| Native health/audio: #477, #560 | Configuration, native transport admission and actual quality remain separate; whole-utterance buffering, 30-second refusal and no silent text/TTS fallback are explicit | Does not claim duplex, browser/device qualification or an engine repair |
| Independent deployment and external Agent: #547–#548, #553, #556, #568 | Agent + Converse, Agent only and Converse only remain distinct; planned Pi bridge does not imply native task execution | No new topology/bridge implementation or installed service change |
| Mobile companion: #549–#552, #556 | Explicit host pairing, local offline transcript, receipt-based catch-up, visible history gaps and deliberate foreground voice controls | No account/device credentials, working transport, VPN setup, microphone or Android service claim |
| Saved creations and web artifacts: #554–#555, #561 | Creations retain task/session/revision provenance; UI publication is separate from execution; reopening must not replay effects | Planned organization; does not promote rendered UI to native completion evidence |

## Archive and prototype checks

- All **49,093 original archive members** remain byte-for-byte identical,
  including all 14 original page payloads and existing shapes/media/thumbnails.
  No original board identity, geometry, interaction or illustration changed.
- Exactly **1,222 new members** add one page and its native shapes. There are
  **34 new boards**, 744 native editable text shapes, and 136 prototype links.
  The complete master has 15 pages and 403 real boards, excluding root sentinels.
- Every new child belongs to its actual parent frame and fits inside that
  frame's declared geometry. Every new navigation target resolves to a real
  board. Motion-on links use 220ms dissolve/ease-in-out; motion-off links omit
  animation. Desktop boards are 1536×1024; mobile boards are 420×900.
- ZIP CRC checks pass. There are no duplicate or traversing member paths.
  No flattened screenshot replaces editable controls. Four versioned SVG
  previews are rendered from the same added native text/vector records.
- The design index, running-task/pending-steer view, Converse instructions and
  mobile offline catch-up were visually checked in a browser. Browser fonts can
  fall back to system fonts; native text records use the master's Inter family.
  This is selected preview verification, not exhaustive interaction testing.
- The exact LFS object was uploaded by the normal repository-authenticated
  batch/basic protocol. Its complete remote download matched the declared byte
  count and SHA-256 before the repository pointer was prepared.

The [machine-readable review](../assets/penpot/native-work-sync-review.json)
retains board identities, archive comparison counts and the bounded publication
receipt. It contains no credentials or signed URLs. The earlier
[Options synchronization record](penpot-options-sync-plan.md) remains historical
evidence for the unchanged 14-page source.

The first native-work candidate used `type` for its 68 new animated transitions.
An audit against the exact producer version's
[Penpot 2.18.5-RC1 animation schema](https://github.com/penpot/penpot/blob/f44566963daf61fce4d8608e7c5e8c27c5853a88/common/src/app/common/types/shape/interactions.cljc#L75)
found that the required discriminator is `animationType` (#598), as in the
original master. The current archive corrects only those 68 added shape
members. Every original member and all static previews remain unchanged.
The review record retains the first candidate's failed grammar check, digest
and publication receipt alongside the corrected candidate's full download
verification. These pinned field/value checks do not execute Penpot's complete
schema or qualify native import, text layout or actual prototype playback.

## Remaining acceptance

No connected Penpot editor was available. Import the updated archive through
Penpot's normal import flow to qualify native text layout and linked
prototypes. The export's source revision metadata is intentionally unchanged;
it is not evidence of an online save. No new service feature, actual speech
quality, mobile connection, hardware playback or task success is established
by these design references.
