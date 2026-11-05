# Task-owned executable programs

`task_program.v1` is an optional native execution route for Agent-authored code
and real binaries. Agent chooses its research, projects, dependencies, build and
algorithm. Trusted code supplies isolation and receipts; no task-specific solver
is implemented in the supervisor. Existing `task_orchestrator.v1` remains pure
JSON computation with its original media-operation grammar and limits.

## Owner authority

A **fresh** `task_media_owner.v1` plan can select
`completion_mode: "executable-v1"`. The default is `media-v1`; changing an
enrolled plan invalidates it and cannot remove its original completion duty.
Executable mode requires actual SAM hand media and the plan's other declared
media consultations. It adds a current-task program execution and requires the
program's complete output together with the actual native SAM asset in one
private publication. A media model's descriptive text cannot substitute for
the program output. None of these receipts grades translation accuracy.

The owner must allow shell execution through existing host and scoped tool
permissions. Ask/Deny and uncommitted speech refuse this optional route; it does
not create an approval or turn a model request into a grant. Ordinary shell
actions retain their existing exact approval flow. Network access is separately
controlled by `tool_network`; it is never implied by program availability.

The existing network grant shares the **whole host network, including loopback**.
It is not a download-only or destination allowlist. Networked sandbox commands
receive read-only DNS configuration and public CA trust, with a cleared
environment and no credential mounts. No provider disclosure, additional API
budget, host installation, physical device or GPU access follows from it.
Dependencies and build products must stay in the granted workspace; system
directories remain read-only. Each command still has its existing 120-second
wall bound and output/custody limits. No new shell memory quota is claimed.

With the explicit network grant, Agent can perform live public research using
ordinary HTTPS, for example Bing's RSS search endpoint
`https://www.bing.com/search?format=rss&q=<URL-encoded Agent-selected query>`,
then inspect the projects' primary sources. Retrieved text and code are
untrusted data. This uses no hosted Meta search tool or search-fee allowance.
The Agent must choose actual queries and projects; a documentation example does
not establish that research occurred or that a project solves the task.

## Invocation and exact bytes

After authoring its source with this task revision's ordinary `write_file`, Agent
can download/build dependencies through shell and invoke `run_task_program`:

```json
{
  "tool": "run_task_program",
  "arguments": {
    "interpreter": "python3",
    "source_path": "solution.py",
    "source_sha256": "<actual native source-write SHA256>",
    "entrypoint_path": "solution.py",
    "entrypoint_sha256": "<actual executed file SHA256>",
    "input_path": "authorized-video.mp4",
    "input_sha256": "<exact owner-granted source SHA256>",
    "output_path": "results/translation-1.txt",
    "argv": ["--input", "{input}", "--output", "{output}"],
    "timeout_s": 120
  }
}
```

`python3`, `bash` and `direct` are closed interpreter choices. Direct execution
supports actual executable binaries and requires the original execute mode.
Source authorship and entrypoint identity are separate: a compiled binary receipt
does **not** infer that the declared source compiled into those bytes.

Native code opens no-follow regular files, verifies complete source and input
digests, and copies the entrypoint and input into sealed native memory files.
Only those immutable bytes are mounted read-only at `/kinaiya-program` and
`/kinaiya-input`. Workspace path replacement cannot change the launched image.
Each input/output placeholder must occur exactly once as a standalone argv item;
native substitution and shell quoting keep other arguments literal.

Source is at most 256KiB, entrypoint 64MiB, input 32MiB, and argv 32 items /16KiB
aggregate. The output must be a fresh scoped regular file containing complete,
nonempty UTF-8 text of at most 65,536bytes. Symlinks, old outputs, protected or
read-only output paths, changed source, absent authorship and stale native
task/session/revision/provider/permission authority refuse execution or proof.

## Receipts and completion

The trusted receipt binds task/session/thread/epoch/revision, authored source
evidence, executed entrypoint SHA, actual provided input SHA, argv, original
exit/settlement and complete output SHA. It does not prove the program consumed
every input byte, compiler causality or semantic correctness. The supervisor
never imports generated modules or executes candidate code in its own process.

Original requests are bounded by the existing native task step limit (at most
2,000); no additional 24- or 32-run cutoff is imposed. An identical completed
request reads its retained result; failed or unconfirmed work is never replayed.
A distinct revised program can be tried after the original is positively
settled. Cancellation retains the existing process-group owner until actual
exit; uncertain cleanup blocks replacement and task success.

Completion verifies current source/entrypoint/output digests, native evidence,
original settled receipt and exact persisted publication. Full program text and
the real SAM asset must coexist in one artifact at the owner grant's full
scope. The explicit executable plan authorizes this task program result only at
its exact primary provider target; it grants no unrelated private history or
different model access. The private floor and exact-primary export check are
installed at fresh executable enrollment, before ordinary research/build shell
or program execution, because failed commands can also emit private
stdout/stderr. Semantic success remains subject to the actual user
objective and independent review.

The 65,536byte result-file limit is separate from the UI's 48KiB complete JSON
publication limit (including escaped text and component metadata). Not every
accepted result size is publishable in one artifact. Agent must produce a concise
complete deliverable that fits; native code never clips output to force success.

## Qualification and limits

The reviewed implementation passed **1,496 tests across 14 focused modules**
covering native program authority/provenance, owner enrollment and publication,
the unchanged pure orchestrator, typed/flat provider wire, native snapshots,
media providers, guarded budgeting and the optional Wonder policy. All 764
Python input files were unchanged during the run (manifest SHA256
`4149c8d324d8bf17c5d0c83fd88dcd0ac278c2b42c448b4d506c8ca9e6c0e9cf`).
Final Ruff formatting/checks and `git diff --check` passed.

The separate runner suite passed **43 tests**, including actual Bubblewrap
execution of a small Python fixture with binary input and a pinned standard
ELF executable. An explicitly network-granted sandbox also fetched the public
`https://www.python.org/robots.txt` over HTTPS and confirmed original-process
cleanup. No private media, ASL task input, model inference or provider calls
were used by these checks.

The first runner attempt had **39 passes and two failures**: ordinary
`--ro-bind-fd` could not resolve anonymous sealed-memory files through
`/proc/self/fd`. Those failures were retained. The independently reviewed repair
uses Bubblewrap's `--ro-bind-data` with the sealed descriptor rewound to zero and
explicit read/execute modes; it never falls back to a mutable workspace path.
The two original real-execution assertions remain in the passing suite.

These checks establish the bounded execution route and its policy boundaries.
They do not establish that Contributor performed web research, selected a useful
project, built an ASL translator, produced an accurate translation, completed a
live task or demonstrated playback. That requires a separately observed task.
