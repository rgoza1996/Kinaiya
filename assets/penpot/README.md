# Kinaiya Penpot design

This folder contains the editable [Converse and Agent Penpot file](Kinaiya-Converse-Agent.penpot).
It is a design draft based on the [Converse](../UIUX%20Concept/kinaiya-converse-moonlit-concept.png)
and [Agent](../UIUX%20Concept/kinaiya-agent-workspace-concept.png) concepts and an
unfinished Figma export. It explores intended interactions; it is not a claim
that every pictured feature is implemented. The project, conversation, file,
media, and status examples are illustrative.

These early desktop exports provide a quick overview; the Penpot file is the
editable source and contains many more screens:

| Converse home | Agent workspace |
| --- | --- |
| ![Converse home design](previews/converse-home.png) | ![Agent workspace design](previews/agent-workspace.png) |

## Open and edit

1. Download `Kinaiya-Converse-Agent.penpot` from GitHub (use **Download raw file**).
2. Open Penpot, choose **Import file** in the dashboard, and select the download.
3. Open the imported file and choose a page in the left sidebar. Select layers to
   edit them; use the **Prototype** tab and **View mode** to inspect linked flows.

The current October 30 sleeping-state export contains 12 pages and 81 top-level
boards. Its SHA256 is `2cf97cfe945a22dfebb358d96928f4aa0c6b658fc0fa97e32a80bf10b95d64d8`.
The first page, **00 · Home & Converse · Updated**, contains 26 connected boards
for Home, optional Converse, model setup, existing sidebar destinations and the
owner-off sleeping Home state.
The app implementation follows [ADR 0059](../../docs/adr/0059-home-and-optional-converse.md);
unsupported model switching, camera and library controls are not working runtime
features merely because they appear in this prototype.

Text, interface shapes,
controls, and icons are editable Penpot layers. The moonlit illustration and
mascot expression references are replaceable raster images, not editable vector
artwork. The expression boards are static design references, not a playable
sprite animation or a demonstration of live TTS synchronization.

## Page guide

| Page | Purpose and key boards | Design status |
| --- | --- | --- |
| **00 · Home & Converse · Updated** | Moonlit Home, optional close-up Converse, model/conversation setup, listening and connected sidebar destinations. | Current owner direction. Illustrative models, tasks and library contents remain examples. |
| **00 · Foundations** | Colors, typography and example controls used as a starting point for the design system. | Draft foundation; not a complete component library. |
| **01 · Converse** | Moonlit home, listening state, and an Options copy for the Home flow. Call and end-conversation links demonstrate the voice entry flow. | Concept-based draft. The Home and gear icons link between the local Home and Options boards. |
| **02 · Agent** | Agent workspace while a task runs and a review/approval state, based on the Agent concept. | Concept-based draft; task details and outcomes are examples. |
| **03 · Converse flows** | Voice-message review, text conversation, history, voice options, appearance, microphone unavailable, and memory/privacy. | Draft screens and example states; not all controls are linked. |
| **04 · Agent flows** | Sessions, task details, an action-specific approval review, projects, files, tools and MCP, harnesses, connection settings, and a new-task empty state. | Draft flows; examples do not establish actual service capabilities or task results. |
| **05 · Narrow screens** | Narrow Converse home/history and Agent sessions/approval layouts. | Responsive starting points; other flows and device sizes need further design. |
| **06 · Agent concept** | A separate editable Agent workspace composition following the supplied concept's harnesses, session list, task conversation and voice-context inspector. | Concept recreation; sample content is illustrative. |
| **07 · Options & menus** | Workspace menu, options overview, voice/audio preferences, and character/motion preferences. | Draft. Selected links connect boards on this page. |
| **08 · Projects & workspace** | Project overview, folder contents, file preview, and a new-project form. | Draft. Selected folder and preview paths are linked. |
| **09 · Generated media** | Media gallery, image and audio previews, and an empty state. | Draft. Selected gallery-to-preview and back links are available. |
| **10 · Mascot interaction** | Tell listening and Call listening, thinking, and speaking states using close-up character references. | Static reference boards; labeled preview controls step through selected Call states. |

## Prototype notes

Prototype links demonstrate selected paths rather than every control. In view
mode, use the page selector to move between Penpot pages; ordinary board
navigation is scoped to the current page. On **01 · Converse**, the gear opens
the Options copy and the Home icon returns to Converse Home. The canonical
Options exploration remains on **07 · Options & menus**; edits to one copy do
not automatically update the other.

Additional responsive layouts, error and permission states, interaction links,
and refined character animation remain design work. Keep sample labels and
status indicators distinguishable from the behavior of the running product.
