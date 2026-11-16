# Native stop-tick observer correction (#640)

The first PR #635 CI run, `43756104574` at
`e749c02bce5fe69e3434cab37f184675a15348c4`, passes Python 3.14 but fails the
3.12 job `132940426610` at the old Converse recovery test's 300ms observer.
That job records 15,021 passed, one failed, nine skipped, 49 live deselected and
two subtests passed in 2771.11 seconds. The actual healthy-call latency is not
retained; a runtime or standing-outcome regression is not established by that
trace. Issue #640 was filed before changing the test.

The test correctly restores the original one-second native allowance after a
deliberately hanging first stop batch. Its external 300ms wait nevertheless
cancels a healthy second tick before that native allowance. A new controlled
400ms delay, followed by actual local/HTTP journal I/O, is within the original
one-second allowance and independently reproduces the mismatch: **two failed,
four passed in 3.11 seconds**. No model is involved.

Only the external healthy-tick observer changes. It now covers one original
stop-batch budget plus the following healthy transcript-read budget and the
existing 300ms watchdog margin. Production deadlines, the first hanging batch's
20ms fault timer and 300ms watchdog, batch size, native I/O, call counts, exact
stop receipts, uncancelled healthy task ownership, retained hanging request and
zero-inference assertions remain unchanged. The controlled delay is retained.

The complete recovery module passes **162 cases in 90.93 seconds** on Python
3.12.3. All six local/HTTP stop-budget cases also pass on the checksum-pinned
ordinary CPython 3.14.10 build in 4.45 seconds; these six overlap the module
total and are not added to it. Ruff and format checks pass. The earlier 783
outcome-related cases cover separate modules and unchanged runtime source.
Remote exact-head CI is still required before merge.

[Verification](verification.json) pins changed test bytes and complete retained
logs. This is a test observer correction, not a production-budget increase or
a model-quality gate. The original CI observation remains in the record.
