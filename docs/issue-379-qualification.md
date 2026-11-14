# Issue 379: provider fences and native publication

Baseline inspected: main `f863129cc7b3a7c9e2272d10f9aa6ae78fba65d7`.
The provider return fence merged in #375; #509 later added a test named
`test_caller_native_publication_never_emits_converse_or_routing_on_late_or_cancelled_media`.

The actual provider tests cover caller cancellation, original deadlines,
response/body/cleanup resistance, exactly one original close, explicit unknown
settlement, pre-existing cancellation, normal success and both media modes.
The existing final-fence fixture explicitly describes its exchange bridge as a
protocol double, not a native Session producer or physical-cleanup proof.

The #509 test awaits that fixture's provider task, manually appends a Converse
or routing event to a real `Store`, and manually appends to an
`agent_dispatches` list. Its negative cases prove the provider does not return a
value to that harness. They do **not** invoke the production Converse/Decisions
publication owner or an actual Agent dispatch. A real Store alone does not turn
the hand-written caller into the production caller.

On this baseline, `MediaSessionOwner._control` still refuses `start` and
`resume` with `unavailable`. `NativeLlamaMediaProvider.complete_media` has no
production service caller; `services/decisions/media_classifier.py` contains
pure projection/parsing functions. The absent native worker path remains a
dependency of #204. Do not enable it or manufacture cleanup receipts just to
close #379.

The self-hosted `suite=media` selection therefore verifies the existing real
provider, classifier and final-fence tests, plus the actual Session missing-
runtime refusal tests, on both CPU architectures. This preserves a useful
regression baseline while keeping the final acceptance gap explicit.

Before #379 can close, compose the actual native worker/caller under #204 and
test that path with controlled cancellation-resistant transport/body/original
cleanup. Observe the real publication owner and Agent ingress, with a healthy
positive control that proves each observer is connected. Verify both modes,
caller cancellation versus original timeout, currentness, one original close
and explicit unknown settlement. Review and run CI on the final integrated
tree. Passing the current harness or an architecture smoke test is not that
evidence.
