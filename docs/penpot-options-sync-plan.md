# Options: editable Penpot synchronization

Updated October 31, 2026 for [#208](https://github.com/rgoza1996/Kinaiya/issues/208).
**Status: applied to the existing master, selected paths visually reviewed, and
final native archive inspected. The Git LFS object was uploaded and its full
remote download rehashed successfully; the pointer/docs are submitted by PR.**
This increment synchronizes provider/catalog, Agent models, immutable notes and
three-tool scoped permissions. It does not complete the full
[Options delivery goal](options-delivery-plan.md).

## Exact master and export

| Identity | Value |
| --- | --- |
| File | `aed0c863-4a2a-8195-8008-defdd82e75a3` — **Kinaiya — Master design** |
| Team | `aed0c863-4a2a-8195-8008-de835100d87c` |
| Project | `aed0c863-4a2a-8195-8008-de835102e964` |
| Current page | `1521d820-51cc-4a49-a496-12d9c823c47e` — **00 · Home & Converse · Updated** |
| Final archive revision | 53, modified `2026-11-01T03:41:42.925373Z` |
| Export | `penpot/export-files` v1, Penpot `2.18.5-RC1` |
| Git LFS file | `assets/penpot/Kinaiya-Converse-Agent.penpot` |
| Size | 228,277,854 bytes |
| SHA-256 | `ba1f0e61d133eccb6539c3aee5504292a4ed61e77416e63e99fd98ef27e3f8be` |

The previous tracked archive was revision 50, 213,963,957 bytes, SHA-256
`4e53fc62fff1b897b0a89e2d8997cc84a6dc6c00328f4e4fcc7ad9abdb946b22`.
The final export has fourteen pages and 329 real top-level boards, excluding
fourteen root sentinels. The current page grows from 189 to 251 real boards.
All changes were made in the same master; no replacement draft was imported.

## Applied content and source mapping

| Area | Applied design | Native/source reference |
| --- | --- | --- |
| Provider profiles | Declaration forms, exact generations, Meta text, native LM Studio, compatible and Laya entries, explicit edit/remove and unknown outcomes. | [ADR0086](adr/0086-owner-provider-catalog.md), [catalog API](provider-catalog-api.md), `options_provider_editor.dart` |
| Credentials | Imported key/reference forms, rotation into immutable generations and explicit profile rebinding; synthetic values only. | Same editor and protected native credential owner |
| Agent models | Host versus saved generation, exact configuration identity, retained unavailable generations and explicit new-generation choice. | [ADR0089](adr/0089-agent-catalog-models.md), `options_model_editor.dart` |
| Harness and auxiliary | Dedicated exact-harness editors, inheritance/custom states and the five supported auxiliary text purposes. | [ADR0082](adr/0082-task-pinned-auxiliary-models.md), ADR0089 |
| Converse notes | Scope/use, source inspection, immutable creation/revision, dependent-note forgetting and uncertain-operation recovery. | [ADR0085](adr/0085-converse-note-editor.md), `converse_memory_editor.dart` |
| Scoped permissions | Default/Workspace/Session/Task precedence, exact read_file/write_file/shell rules, inheritance, unsaved task edit, receipt uncertainty, same-instance fencing and retired-scope reset. | [ADR0096](adr/0096-scoped-native-tool-permissions.md), `options_tool_permissions.dart` |

Flutter filenames above are within `apps/kinaiya_flutter/lib/ui/`. Actual editors
are embedded scrollable forms. Separate detail boards illustrate their states;
they do not introduce runtime modals or Home-bubble navigation. All records,
models, revisions, digests, transcripts and credentials are synthetic examples.
Saving a declaration is not qualification, a probe, a role change or disclosure
permission. Allow/Ask/Deny affects one native operation and respects host,
isolation, path, network, speech and disclosure ceilings.

The existing gear illustration, Kinaiya's approved artwork, shell navigation,
board identities and editable native/vector interface layers remain. No
flattened screenshot was placed over an editable page.

## Application and recovery record

The guarded local patches inspected exact file/page/board identities and
baseline child geometry before applying. Sixty original content areas received
provider/model/note changes, with fifty new detail boards. Two existing
Permissions areas received scope changes, with twelve more paired state boards.
Motion-on navigation uses 220ms dissolve/ease-in-out; motion-off is instant.

The following recovery/final versions were saved in Penpot:

1. **Before native Options provider, memory and model sync**.
2. **Native Options · provider catalog, immutable notes and exact Agent model choices** (revision 51).
3. Scoped permissions pre-change recovery and **Scoped native tool permissions · four scopes and exact receipt recovery** (revision 52).
4. **Before scoped permission stacking repair**.
5. **Scoped permission text and controls · corrected stacking** (revision 53).

Visual review caught labels hidden beneath imported backgrounds in the scoped
patch. A guarded repair reordered 840 existing interface layers on 14 boards.
Archive comparison confirms only child stacking order changed: shape identities,
text, geometry, interaction definitions and artwork remained exact. The flawed
intermediate export was not selected for repository publication. These local
patches are single-use and must not be rerun against the changed master.

## Collected evidence

The [archive review record](../assets/penpot/options-sync-review.json) records:

- Same 14 page identities; other 13 page payloads byte-exact.
- All 267 original real board identities, names and geometry preserved.
- Exactly 62 added detail/state boards on the current page.
- Shapes outside the 62 existing content targets unchanged.
- All 100 uploaded media metadata/blob paths byte-exact, including mascot art.
- All1,126 first-patch interactions preserved; all 230 new permission links
  resolve to real boards with valid motion (115 dissolve, 115 instant).
- Stacking repair preserves every shape and only permutes existing child lists.

Selected desktop paths were reviewed in the browser: Models, Providers, exact
LM Studio profile editing, corrected Permissions, the Task unsaved edit and
its unknown-save route. This is selected visual/prototype review, not exhaustive
interaction coverage or application-runtime acceptance. Static archive analysis
is not a test-suite result. No model, credential, camera or microphone probe was
part of this design synchronization.

## Publication and remaining design work

The final object was published through the standard repository-authenticated
Git LFS batch/basic protocol. Its full remote download matched the size and
SHA-256 above. The small tracked LFS pointer and these notes follow a normal
reviewable PR, preserving branch protection and the user's primary checkout. The older ignored publisher
that wrote main directly must not be used.

Historical Options copies on **01 · Converse** and **07 · Options & menus** stay
historical as labeled in the [design README](../assets/penpot/README.md). They do
not synchronize automatically. The approved overview mascot remains unchanged.
The following require their own source-aligned design increments: new Converse
text-model editing, ordered auxiliary fallback routes, Search/individual MCP
rules, and the remaining Options inventory. Unsupported or unqualified runtime
features must remain explicitly identified; this export does not deliver them.
