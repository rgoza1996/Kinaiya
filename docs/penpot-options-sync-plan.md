# Options: editable Penpot synchronization

Updated November 3, 2026 for [#208](https://github.com/rgoza1996/Kinaiya/issues/208), [#286](https://github.com/rgoza1996/Kinaiya/issues/286), [#352](https://github.com/rgoza1996/Kinaiya/issues/352), and [#382](https://github.com/rgoza1996/Kinaiya/issues/382).
**Status: applied to the existing master, selected paths visually reviewed, and
final native archive inspected. The Git LFS object was uploaded and its full
remote download rehashed successfully; the pointer/docs are submitted by PR.**
This increment synchronizes provider/catalog, Agent models, immutable notes,
three-tool scoped permissions, the November 2 UI/UX review usability
improvements (`#286–#310`), the unlimited custom OpenAI-compatible `.env`
provider & model discovery prototype boards (`#352–#353`), and the 14-step CLI
Installer & Setup Wizard decision tree with all choice branches (`#261`, `#335`,
`#336`, `#381–#402`). It does not complete the full
[Options delivery goal](options-delivery-plan.md).

## Exact master and export

| Identity | Value |
| --- | --- |
| File | `aed0c863-4a2a-8195-8008-defdd82e75a3` — **Kinaiya — Master design** |
| Team | `aed0c863-4a2a-8195-8008-de835100d87c` |
| Project | `aed0c863-4a2a-8195-8008-de835102e964` |
| Current page | `1521d820-51cc-4a49-a496-12d9c823c47e` — **00 · Home & Converse · Updated** |
| Final archive revision | 58, modified `2026-11-03T23:10:00.000000Z` |
| Export | `penpot/export-files` v1, Penpot `2.18.5-RC1` |
| Git LFS file | `assets/penpot/Kinaiya-Converse-Agent.penpot` |
| Size | 232,727,891 bytes |
| SHA-256 | `ec2dacaf99160eff3bc19eca26a46ab35f8791451e4dd7288f4a714a4be180f7` |

The previous tracked revisions were revision 56 (`228,859,935` bytes, SHA-256
`739417a5f93a0f46783032fa18b599f8a83e02fdad18519d76f605934a584eec`), revision
55 (`228,843,262` bytes, SHA-256
`198427eef1e7aafd65b0ef3013ac76e2fabd43cbaefdff6126c5e2a3f3325bf0`), revision
54 (`227,769,664` bytes, SHA-256
`892a4867e0c613b5d9f4e3d97274e13e6f0581a7a7da7977c7592154a15542c5`), revision
53 (`228,277,854` bytes, SHA-256
`ba1f0e61d133eccb6539c3aee5504292a4ed61e77416e63e99fd98ef27e3f8be`) and
revision 50 (`213,963,957` bytes, SHA-256
`4e53fc62fff1b897b0a89e2d8997cc84a6dc6c00328f4e4fcc7ad9abdb946b22`).
The final export has fourteen pages and 369 real top-level boards, excluding
fourteen root sentinels. The current page grows from 189 to 291 real boards.
All changes were made in the same master; no replacement draft was imported.

## Applied content and source mapping

| Area | Applied design | Native/source reference |
| --- | --- | --- |
| Provider profiles | Declaration forms, exact generations, Meta text, native LM Studio, compatible and Laya entries, explicit edit/remove and unknown outcomes. | [ADR0086](adr/0086-owner-provider-catalog.md), [catalog API](provider-catalog-api.md), `options_provider_editor.dart` |
| Custom OpenAI-compatible `.env` APIs (`#352`) | Unlimited custom OpenAI-compatible profiles (`p01..p06+`, no 5-slot cap) persisted in user-local `0600` `.env` (`KINAIYA_CUSTOM_PROVIDER_IDS`), custom endpoint path fields (`chat_completions_path`, `models_path`, and optional engine-specific endpoints such as `/v1/systemone` for `llama.cpp` vs standard `/v1` for `LM Studio`), optional maximum output tokens (blank = up to model), global/per-model reasoning effort in `Models` instead of per-provider reasoning effort, Add/Edit Custom API, masked key rotation, `GET {base_url}{models_path}` discovery, manual model ID fallback, `(provider_profile_id, model_id)` disambiguation, and referenced-profile removal confirmation. | [#352](https://github.com/rgoza1996/Kinaiya/issues/352), `services/settings/custom_providers_env.py`, `options_provider_editor.dart`, `options_model_editor.dart` |
| CLI Installer & Setup Wizard decision tree (`#261`, `#335`, `#336`, `#381–#402`) | 28 paired prototype boards (14 motion-on, 14 motion-off) and 702 prototype links covering every choice and branch in `install.sh` and `kinaiya` CLI onboarding: channel (`stable` vs `nightly`), install root (`~/.local/share/kinaiya` vs `./kinaiya` vs custom `--workspace`/`--data-dir`), strict APT no-upgrade/no-remove solver & preflight refusals, `[y/N]` plan consent, 5-stage human progress & `--json` mode, `--no-setup` / cancelled / existing-config preservation, Quick vs Advanced vs `--section` wizard modes, `:back`/`:cancel` navigation, local vs cloud vs searchable model picker, Converse toggle & independent Converse/Decisions role profiles, cloud disclosure consent & masked API key entry (`0600 providers.env`) vs env-var reference, first-run named custom `.env` profile creation, bounded connection verification probe states (`not_checked`, `verified`, `auth_failed`, `unreachable`), completion summary & readiness badges, `kinaiya start` / local sign-in ticket exchange / `kinaiya doctor` / `rotate-owner`, and `update` / `recover` / `rollback` / `uninstall` lifecycle branches. | [#261](https://github.com/rgoza1996/Kinaiya/issues/261), [#382](https://github.com/rgoza1996/Kinaiya/issues/382), `install.sh`, `services/distribution/cli.py`, `services/distribution/wizard.py`, `services/distribution/wizard_flow.py` |
| Credentials | Imported key/reference forms, rotation into immutable generations and explicit profile rebinding; synthetic values only. | Same editor and protected native credential owner |
| Agent models | Host versus saved generation, exact configuration identity, retained unavailable generations and explicit new-generation choice. | [ADR0089](adr/0089-agent-catalog-models.md), `options_model_editor.dart` |
| Harness and auxiliary | Dedicated exact-harness editors, inheritance/custom states and the five supported auxiliary text purposes. | [ADR0082](adr/0082-task-pinned-auxiliary-models.md), ADR0089 |
| Converse notes | Scope/use, source inspection, immutable creation/revision, dependent-note forgetting and uncertain-operation recovery. | [ADR0085](adr/0085-converse-note-editor.md), `converse_memory_editor.dart` |
| Scoped permissions | Default/Workspace/Session/Task precedence, exact read_file/write_file/shell rules, inheritance, unsaved task edit, receipt uncertainty, same-instance fencing and retired-scope reset. | [ADR0096](adr/0096-scoped-native-tool-permissions.md), `options_tool_permissions.dart` |
| November 2 UI/UX review (`#286–#310`, `#353`) | Home Connect/Setup priority, hidden Converse recovery (`#353`) & prerequisite guidance (`#287–#289`), Agent empty state & objective/state/evidence-first header (`#290–#292`), Converse voice state chips, compact model/privacy summary & causal handoff/task progress (`#293–#298`), Options search, orientation, setup shortcuts & grouped role copy (`#299–#308`). | `home_view.dart`, `moonlit_home.dart`, `character_view.dart`, `agent_chat.dart`, `options_view.dart`, `options_model_editor.dart` |

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
6. **November 2 UI/UX review usability sync · Home, Converse, Agent and Options text layers** (revision 54).
7. **Unlimited custom OpenAI-compatible .env API prototype boards & hidden Converse recovery sync** (revision 55).
8. **Custom endpoint path fields, optional blank max output tokens, and global/per-model reasoning effort sync** (revision 56).
9. **14-step standalone OS terminal CLI Installer & Setup Wizard decision tree with multi-color ANSI syntax highlighting, multi-tone deterministic 68×14 ASCII Home Moon, and all choice branches** (revisions 57–59).

Visual review caught labels hidden beneath imported backgrounds in the scoped
patch. A guarded repair reordered 840 existing interface layers on 14 boards.
Archive comparison confirms only child stacking order changed: shape identities,
text, geometry, interaction definitions and artwork remained exact. The flawed
intermediate export was not selected for repository publication. These local
patches are single-use and must not be rerun against the changed master.

## Collected evidence

The [archive review record](../assets/penpot/options-sync-review.json) records:

- Same 14 page identities; other 13 page payloads byte-exact.
- All 267 original, 329 revision-54, and 341 revision-56 real board identities, names and geometry preserved.
- Exactly 102 added detail/state boards on the current page (including 12 paired Custom API `.env` prototype boards in revision 55 and 28 paired standalone-terminal CLI Installer & Setup Wizard prototype boards in revisions 57–58).
- Shapes outside the updated content targets unchanged.
- All 100 uploaded media metadata/blob paths byte-exact, including mascot art.
- All 1,126 first-patch interactions preserved; all 230 permission links,
  286 Custom API prototype links, and 668 CLI Installer prototype links resolve
  to real boards with valid motion (220ms dissolve on motion-on, instant on
  motion-off).
- Stacking order places background groups first, editable text layers second,
  and clickable interaction rects last.

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
