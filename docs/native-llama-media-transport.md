# Native llama media adapter boundaries

Related: [media protocol](media-protocol.md), [#359](https://github.com/rgoza1996/Kinaiya/issues/359), [#379](https://github.com/rgoza1996/Kinaiya/issues/379), and [#434](https://github.com/rgoza1996/Kinaiya/issues/434).

The adapter implements pinned, buffered local multimodal input for Converse chat and direct SystemOne Decisions. Its parsed output is text or typed decisions. This does not implement native speech output, duplex conversation, a decoder, or the native Session consumer/producer bridge. Those integrations and their physical-cleanup qualification remain separate requirements.

## Result admission and original settlement

A newly requested caller cancellation is checked relative to the cancellation count captured when this exchange starts. A count predating the exchange is not a new cancellation. Current source/transport guards run before checking that delta, including after native registration and before HTTP dispatch.

Registration that suppresses cancellation does not permit a new HTTP request. Its returned original registration is still retained and conservatively settled; zero HTTP sends does not prove that its native task, body, readers or publications stopped.

After the last awaited original settlement, current source/transport identity and new cancellation are checked again. The adapter also checks the original timeout's absolute `when()` against the actual loop clock, since its context may already have exited before settlement returns. No await separates this final check from first publication of the consumer disposition and usable result.

Verified physical observations remain independent of consumer disposition. A verified completed physical exchange can have a cancelled, timed-out or failed consumer result. Conversely, cancellation without a returned proof bundle retains the same original registration as unknown with no invented body-close or cleanup proof. Settlement is not retried or replaced to obtain a cleaner outcome.

## Prepared request validation

The already bounded `raw_body` is decoded first and must retain its exact canonical encoding and digest. Validation compares independent caller-owned `body` data only along this decoded blueprint, checking exact builtin JSON types, lengths, shape, recursive dictionary order and scalar identity. A mismatched large or cyclic graph is not independently serialized, encoded or copied. Float signed zero remains distinct, matching the original JSON-byte requirement.

Only plain `dict`, `list`, `str`, `int`, `float`, `bool` and `None` values are accepted in the independent graph. Nested subclasses or custom objects are refused instead of invoking caller comparison hooks. This is an explicit tightening of the Python-facing prepared-request input domain. The existing wire, media, provenance and mode limits remain unchanged. The returned request uses the detached decoded graph.

Independent SystemOne `questions` retain their previous recursive order-insensitive dictionary matching, with caller key types and lengths checked before lookup. This does not relax the ordered identity of the questions inside the actual request body.

## Raw task description limit

`MAX_TASK_DESCRIPTION_INPUT_CHARS` is explicitly 16,000 characters, using the existing `MAX_TEXT_CHARS` bound. Dictionary task descriptions longer than that are refused before full whitespace split/join, and the whole projection fails; task IDs, associations and revisions are never dropped to fit the limit.

This intentionally changes the previous acceptance of arbitrarily long raw descriptions. Within the supported input limit, existing behavior remains: `None` becomes empty text, whitespace is collapsed, and the normalized description uses the existing 360-character prefix. Validation of that resulting description is unchanged; discarded suffixes do not acquire a new full-raw UTF-8 or NUL validation rule.

## Verification scope

Focused checks use the actual provider with controlled HTTPX transports and protocol bridge records. A successful synthetic bundle is not proof of an actual native Session producer, model worker, decoder, device or remote cleanup. Exact source, command, positive controls, retained negative results and integration/CI status must accompany any qualification claim.
