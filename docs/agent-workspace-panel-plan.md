# Agent workspace panel and conversation navigation plan

Status: the responsive chat width, resizable sidebars, recorded tool-activity
groups, and navigation among loaded recorded messages are implemented in the
current UI increment. Files and Browser panels remain planned. An interactive
terminal is deferred.

## Goal and present foundations

Keep Agent Chat usable on ultrawide and narrow screens while letting the owner
inspect work and use workspace tools alongside the conversation. The right-hand
panel should become a tabbed workspace containing Task details, Files and
Browser. An interactive terminal is deferred. Tabs must show real state;
unavailable tools should say why rather than show a simulated working surface. Preserve the two independent left
sidebars, the selected session, draft, approvals, and task controls.

The current Flutter UI has Task details, session navigation, and a journal-backed
chat. Its history loader projects `agent.command` submissions, attributable
`agent.tool` records and `agent.outcome` records into the selected session. It
groups consecutive tool records by task and revision. Shell summaries and output
are recorded, bounded text; the summary may contain only the first 120 command
characters. The native journal records some `agent.tool` outcomes, but not every
possible start or failure is necessarily recorded. Generated Documents and
Media are write-receipt libraries, not a workspace file browser
([ADR 0080](adr/0080-owner-projects-and-generated-libraries.md)). Agent's
bounded shell tool is not an interactive terminal. The native owner can select
an active, identity-pinned workspace ([ADR 0097](adr/0097-workspace-owner-selection.md)).

## Browser candidate and deferred terminal

These are considerations for implementation planning, not installed or chosen
dependencies. Do not implement or bundle an interactive terminal in this phase.
Shell tool activity is a read-only record within the conversation, not a terminal.

- For a future native desktop browser panel, evaluate a Chromium-based control
  such as [CEF](https://github.com/chromiumembedded/cef/blob/master/docs/architecture.md),
  including its packaging, update, permission and isolation costs. The current
  web client can embed pages only when their policies permit it; provide an
  external-browser fallback for pages that refuse framing. A user can continue
  using their preferred system browser independently.

## Licensing and distribution considerations

Kinaiya's repository is Apache-2.0 licensed. The following upstream licenses
inform evaluation, but do not clear any particular integration or package:

- [xterm.js](https://github.com/xtermjs/xterm.js/blob/master/LICENSE) and
  [Ghostty](https://github.com/ghostty-org/ghostty/blob/main/LICENSE) use the
  MIT license for their own code. Preserve the applicable copyright and license
  notices when distributing covered copies; inspect selected add-ons and
  transitive dependencies separately.
- [Kitty](https://github.com/kovidgoyal/kitty/blob/master/LICENSE) uses GPLv3.
  Starting a user's independently installed terminal is a different scenario
  from bundling, modifying, linking, or incorporating its code; review the
  actual integration and distribution obligations rather than assuming an
  automatic result. The GPL permits commercial distribution when its terms are
  followed.
- [CEF](https://github.com/chromiumembedded/cef/blob/master/README.md) is
  BSD-licensed and [Chromium's root code](https://chromium.googlesource.com/chromium/src/+/main/LICENSE)
  has BSD-style terms. A packaged browser can include many components with
  their own licenses; Chromium's [license inventory](https://github.com/chromium/chromium/blob/main/tools/licenses/licenses.py)
  illustrates why the complete chosen distribution must be reviewed.

Before distributing any integration, pin its exact versions, inventory the
actual shipped components and their licenses, provide required notices and
source or other materials where applicable, and review branding and other
distribution terms. Obtain qualified legal review for the chosen packaging,
especially GPL or other uncertain cases. These candidates have not been added
as dependencies in this UI increment.

## Delivery slices

1. **Panel shell.** Extend the existing collapsible, draggable right panel to
   support labelled tabs, a visible active tab, close/reopen and keyboard
   access. Keep tab contents and unsent drafts intact when resizing, hiding the
   panel, changing Agent sections, or temporarily shrinking the viewport. On
   narrow screens use a reachable sheet or full-screen tool view. Keep a
   usable conversation width and respect the user's theme and text size.
   Decide whether a panel size is remembered across app restarts before adding
   persistence; never persist private page content as a
   side effect of saving layout.

2. **Activity groups and disclosure control.** The current increment projects
   actual, authenticated journal events for the selected
   session/thread/epoch. Group consecutive related tool events under a
   collapsible summary, with a button that reports expanded/collapsed state and
   reveals individual calls, recorded results and genuine status. Associate
   events with their native task and revision; do not infer a running state or
   successful effect from absent records. Handle missing, older, duplicated,
   delayed and malformed records explicitly. Show recorded shell output in a
   bounded, selectable monospace block with recorded status and a command only
   where the record actually supplies one. The current shell evidence contains
   a possibly shortened summary, not the complete command. Escape literal tool
   output and bound rendering; opening a group performs no tool action. If live start or
   progress events are wanted beyond what the journal records, define and
   implement their native contract and recovery semantics first. While Agent
   Chat is open, the client checks for new records periodically after the current
   history pages are loaded; this is not a live tool execution stream.

3. **Conversation navigation rail.** The current rail has keyboard- and
   pointer-accessible markers for loaded recorded user and Agent messages, using
   native event IDs as targets. It highlights the last selected marker and keeps
   the conversation and composer unobscured. It does not claim markers for
   unloaded pages or continuously track the reader's position. The current API
   reads forward by sequence; a bounded way to locate and load an unloaded target
   remains future work. Reset, session switch, and stale or unavailable history
   must never navigate to another session's content.

4. **Files tab.** Design an owner-authenticated, read-first workspace tree and
   file viewer tied to the selected active workspace identity. Do not turn the
   existing receipt libraries into filesystem scans or resolve old unpinned
   receipts against today's workspace. Define pagination, binary/large-file
   behavior, edits and conflict detection separately. Enforce configured
   read-only and protected paths, traversal/symlink/race defenses, permissions,
   and bounded reads in the native owner rather than trusting the client.

5. **Browser tab.** Choose a supported host integration for Flutter web and
   any desktop targets, with navigation, back/forward, address display and
   useful failure states. Embedded web frames cannot display every site;
   select an appropriate native/browser-process or external-browser fallback
   instead of claiming universal embedding. Isolate untrusted pages from owner
   credentials, local APIs and terminal/file capabilities, and define profile,
   downloads and permission behavior before enabling them. A page or tool
   result cannot grant permission or issue Kinaiya commands.

## Acceptance and verification

- Test ultrawide, ordinary desktop, narrow, enlarged text and custom themes;
  exercise mouse/touch dragging, keyboard resizing, collapse/reopen, viewport
  changes and screen-reader labels without losing the draft or active session.
- With synthetic multi-page sessions, verify ordered activity, honest missing
  data, disclosure state, event deduplication, marker jumps, focus, scroll
  position, session/epoch changes and failure recovery.
- For Files and Browser, specify native authority and lifecycle
  contracts before connecting the UI. Verify unauthorized requests, workspace
  replacement, path attacks, untrusted output/pages, disconnection and actual
  cleanup. Separately test supported host platforms and unavailable fallbacks.
- Keep delivered capabilities and their evidence distinct from this plan in the
  implementation ledger and relevant contracts as each slice ships.
