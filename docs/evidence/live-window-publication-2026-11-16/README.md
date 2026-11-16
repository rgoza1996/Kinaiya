# Native live publication evidence (#623)

This increment preserves original transcript/capture identity and source
partitioning. Adaptive Live stays off. The parent is
73b73c5a5c34694eb76e2006f25c0090ecb98974. Exact commands, interpreter versions,
frozen input/log hashes and results are in [verification.json](verification.json).
The [contract](../../live-window-publication.md) describes bounds and remaining
integration. No real model, microphone or physical-process claim is made.

Final focused checks pass 116 cases on Python 3.13.18 in 14.28s and the same 116 on
Python 3.14.10 in 14.33s. All 862 declared Python inputs remain unchanged over each run.
This includes the unchanged 63 old standing/schema cases and the seven-file
additive pin; all 543 prior schema files retain their bytes. Native controls cover
two-connection atomic visibility, rollback, exact lost-ACK lookup, concurrent
idempotence, owner/mode partition, duplicate input stability, changed raw content/
hints/scope/STT provenance, maximum-length text, reset/restart/deletion, strict
wire bounds, role parity and original retained HTTP read/sole close.

The broader Session/window/wake/media regression passes 714 cases in 408.74s with one
existing Starlette warning. Its 862 inputs stay unchanged. The 86 incremental-stream
regression cases also pass in 6.58s. These runs precede the final HTTP error mapping;
only the two native guard files and its HTTP control changed afterward, as listed
in the receipt. The current v1 Session/window regression passes another 57 cases in 9.11s,
with the same existing warning and all 862 inputs unchanged.

Four first failures were recorded in #623 before repairs:

- Native row columns:3 failed / 14 passed in1.90s. Serialized request/value and
  event/sequence agreed, but protected native thread/epoch/kind disagreement was
  accepted. The repair bounds and compares all relevant native columns. Frozen
  [first controls](first-native-controls.py.txt) retain the original assertions.
- Caller mutation:1 failed / 109 passed in10.15s. A caller could mutate the nested v1
  request after actual send, changing the response comparison. The client now
  detaches its original before any HTTP effect. The frozen
  [client control](first-client-control.py.txt) retains the held original response.
- Direct legacy append:2 failed / 1 passed in0.45s. Backlog filtering prevented old
  admission, but direct native v1 decisions/status could claim a live source.
  Both old publication kinds now refuse an annotated source. The frozen
  [protocol control](first-v1-controls.py.txt) preserves both cases.
- HTTP refusal mapping:3 failures in 1.40s. The new guards raised outside the old
  JournalError boundary, producing 500 for native refusal or malformed windows.
  The repair preserves the original v1 payload validation and maps native live
  refusal to JournalError/409. The [HTTP control](first-http-refusal-control.py.txt)
  preserves all three original wire assertions.

Failures, hashes and exact current results remain attributable in the receipt;
passing synthetic checks do not establish full #623 readiness or model quality.
