# ADR 0070: Share one task-qualified native tool advertisement

Status: accepted for the bounded #171 advertisement repair.

The capability API, seed prompt and Meta wire schema separately enumerated tools.
They could advertise unavailable gateways, including UI publication without
artifact storage and a selector unavailable to a task pinned to the seed runtime.

Build one immutable, versioned snapshot from native configuration and the task's
actual harness pin. Project it into prompt/schema/inspection and bind it through
request-local context. Refuse unadvertised proposals before execution, retain
failed-effect taint and recheck current availability at dispatch. Continue using
existing permissions, disclosure, selected-target catalogs and pinned execution;
availability never grants authority. See the [contract](../native-tool-advertisement.md).

This avoids mutable shared provider state and prompt-derived authority while
preserving standalone provider compatibility and the known protocol enum. It
does not install conventional software, flatten foreign tools into the native
enum, complete the Tool Library or qualify provider/model task quality.
