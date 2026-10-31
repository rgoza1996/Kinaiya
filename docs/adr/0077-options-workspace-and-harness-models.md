# 0077: Options workspace and native harness model defaults

Owner request and acceptance: [#188](https://github.com/rgoza1996/Kinaiya/issues/188).
Design baseline: master Penpot export at `99f7fd1`, especially its gear
illustration, split settings sections and dedicated Harness Customization flow.
This supersedes ADR0053's dismiss-and-forward hub navigation for the main app.

## Options presentation

The main app opens Options as a workspace beside global navigation. Its sidebar
owns category navigation and search. A paired category opens its first section;
the sidebar then lists its sections and Back. Models, Providers and Harness
Customization are separate destinations. The overview shows the approved
Kinaiya/gear artwork and no duplicate category grid. Small screens use a settings
drawer. Close restores the previous workspace.

Home/Converse and Agent remain mounted. Existing appearance, character, voice,
memory, connection and MCP owners render inside Options; their independent
dialog entry points remain compatible. Opening a category never opens Home's
bubbles, changes the selected Agent session, captures audio, runs a task or
calls a model. Metadata reads are authenticated where required. Read failures
remain unavailable rather than displaying design examples as user data.

Harness Edit captures the catalog's exact version ID. The editor offers
inheritance or an explicit configured model profile, with provider and supported
reasoning labels. Dropdowns update the same editor in place. Unapproved harnesses
can be inspected but cannot receive overrides that make them executable.
The provider page includes Meta API and LM Studio guidance. No Meta OAuth flow
or unverified provider/account capability is simulated. Converse/STT/TTS/Decisions
model settings remain with their existing native configuration owners.

## Native model selections

`services.agent.model_settings.AgentModelSettings` stores one Agent default and
up to 1,000 harness overrides in protected `agent-models.sqlite`, mode 0600.
It contains profile references and reasoning labels, not credentials, endpoint
URLs, prompts, permissions or generated code. The host's `providers.agent` is
the reserved `host-default` entry. At most 31 additional `agent_profiles` are
selectable. Their names are bounded labels; invalid optional profiles
are unavailable. Constructing a configured adapter performs no inference or
model loading and does not prove endpoint readiness.

Optional host `agent_reasoning_options` declares allowed labels per named
profile, including `host-default`. Labels are restricted to the existing adapter vocabulary:
`none`, `minimal`, `low`, `medium`, `high`, `xhigh`. Only declare levels that
the actual model and provider support. Without this declaration, the profile's
existing reasoning configuration is used. An override creates a separate
adapter instance; it never mutates an adapter used by another task. The number
of profile/effort instances is bounded by this finite catalog.

For example, the following **configuration fragment** adds one owner-reviewed
Agent profile; credentials and supported context-count transport still need
their existing provider configuration. This is not a working endpoint or a
model recommendation:

```json
{
  "agent_profiles": {
    "review-model": {
      "kind": "meta",
      "model": "owner-selected-model-id",
      "base_url": "https://owner-configured-provider.example/v1",
      "secret_ref": "muse",
      "allow_cloud": true,
      "context_tokens": 32000,
      "reasoning_effort": "low"
    }
  },
  "agent_reasoning_options": {"review-model": ["low", "medium", "high"]}
}
```

Task acceptance resolves the **trusted native session harness**, then captures
the effective provider object and a serialized `model_pin`: profile ID,
provider, model, locality, reasoning and settings revision. New defaults affect
new tasks, including later tasks in an existing session. Accepted, running,
approval-waiting and steered tasks retain their pin. Submit retries retain the
original pin. Historical tasks without a pin remain historically unknown;
recovery never replays a task using a replacement model.
An exact accepted-submit retry reads the retained task before current provider
admission, while retaining native source authority and request identity checks;
see the [task receipt contract](../agent-options-api.md#existing-task-receipts).

Prompt counting/generation, native source admissions, transcript reads, skill,
plugin and MCP disclosure checks all use the task's provider. Changing the
default cannot widen an existing task's content scope, grants or tools. Native
resource policy still applies at each actual inference request. A selected
profile lacking exact complete-request counting refuses normally; compatible
HTTP alone does not qualify an Agent provider. Task details show the captured
identity rather than the current default.

Legacy `/model` keeps its settled-work boundary and updates the same persisted
default. Owner settings use the [versioned API](../agent-options-api.md).
Model changes and task acceptance share the native integration lock. Settings
do not promote a harness or alter its approval/evaluation state. Promotion
quality for a harness/model pair remains separate from selecting a provider.

## Save outcomes and UI ownership

Writes use an expected revision and unique request key. One SQLite transaction
stores the update and its receipt; a matching retry returns the same receipt,
while a different body/key pairing or stale revision refuses. Receipts are
bounded at 10,000; the host refuses further changes when owner maintenance is
needed, rather than evicting an unresolved request. The UI retains up to 16
unresolved endpoint-scoped operations in RAM across Options close/reopen and
same-endpoint reconnect. A display timeout is not failure evidence. Explicit
receipt lookup reconciles a committed operation; an explicit retry reuses the
same request key and exact body. At most two write requests per operation can
remain in flight, including an explicit retry after the display deadline.
Navigation never retries a write. Each response
checks connection ownership before publishing into the current view.

## Evidence and remaining gates

Implementation checks are recorded in the associated PR. Python compilation,
static analysis and a Flutter release build do not establish runtime correctness,
model quality, concurrency/recovery acceptance or physical audio behavior.
The existing navigation and consent fixtures now reflect the persistent
workspace and explicit legacy-host metadata fallback. GitHub Actions runs the
repository's deterministic regression suites and platform builds on this PR;
the PR records each result and revision. No new test cases or local test runs
were added in this implementation session. Further acceptance cases are
native authentication, stale revisions, exact retries, lost responses, settings
persistence, unavailable profiles, current harness eligibility, overlapping
tasks with distinct model pins, steering/recovery, cloud disclosure and all
existing prompt/tool admission guards. UI cases include section navigation,
per-harness identity, in-place dropdowns, retained drafts/audio and narrow/
large-text layouts. These remain gates before production promotion.
