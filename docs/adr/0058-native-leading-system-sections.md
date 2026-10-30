# 0058: Preserve leading system sections for native model templates

Issue [#136](https://github.com/rgoza1996/Kinaiya/issues/136) follows the local
Qwen3.5 2B switch. An empty-history greeting succeeded, but ongoing Converse
failed during exact measurement: its personality and playback qualifications
occupied separate leading system messages. The resident model's Jinja template
rejected the second one with `System message must be at the beginning.`

The LM Studio adapter now joins only the maximal contiguous initial run of
system sections into one native system message, inserting two newlines between
their complete text contents. It neither relocates later system messages nor
promotes user content. The Session journal and logical context projection retain
their original messages, roles, scopes and delivery qualifications.

Normalization happens inside `_measure`, which constructs both the formatter
request and the prediction creation parameters. Thus template-inclusive counting
and generation use identical native history and configuration. Source-mutation
checks and the retained measurement identity still bind the original, unmerged
history. Repartitioning text across source system-message boundaries cannot reuse
an earlier measurement, even if the normalized model input would be identical.

The regression fixture reproduces the strict template error before the fix. It
checks two and three leading sections, empty/Unicode text, repeated assistant
chunks, later system positions, immutable sources and raw-boundary changes.
An opt-in public native probe verifies three system sections, matching terminal
prompt counts, normal completion and confirmed cleanup. Existing cancellation,
disclosure, history-selection and terminal refusal checks remain applicable.

These checks establish compatibility with the tested resident template; they do
not establish conversational quality or physical voice latency.
