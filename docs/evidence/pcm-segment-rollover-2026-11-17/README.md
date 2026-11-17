# Local PCM segment rollover controls (#623)

This continues the unfinished host/Call branch from
`72e2b6287ddc3c86ee9709a0920708b3db34fc60`. Adaptive Live remains off.
The actual CharacterView Call owner and native logical utterance boundary are
still unfinished; these checks qualify local PCM rollover, not the complete
feature or physical acoustics.

`browser_pcm_segment_v1` binds the exact local acknowledged prefix and returns
a strictly newer packet epoch. It preserves the armed microphone, current
speaking hint and every unacknowledged tail sample in one fixed allocation.
An old packet cannot acknowledge the next decoder's samples. Native finish,
paired publication and actual native decoder cleanup remain separate caller
obligations; local rollover grants none of that authority.

The [manifest](manifest.json) retains exact commands, all 329 current frontend
input hashes, per-run scope/deltas, original source references and exact UTF-8
log envelopes. Current Dart and web-build source copies match these inputs;
the dependency lock is unchanged. No package/SDK installation, installed-owner
promotion, physical microphone or live model endpoint was used.

| Current check | Result |
| --- | --- |
| `node --test` over all seven browser audio test entrypoints | 227 passed |
| Incremental PCM codec, actual bridge fixture and native stream client | 22 Flutter tests passed |
| `flutter analyze --no-pub` over five affected Dart files | No issues |
| `flutter build web --release --no-pub --no-web-resources-cdn` in an isolated declared-source copy | Passed in 29.8 seconds; Wasm dry run passed |

The existing missing CupertinoIcons font warning remains in the build log.
The current browser, Dart and release-build log digests are respectively
`e0b8134bf29123810d6fad7c9a224788b2c6bcbbef46c8dcab23e1d681156079`,
`ea52fa6438daa05db49b370051e55c4b42e40aed59988214c1fef8e9be11d53f`
and `3e4322aa6e824c330b3e7c9ea3e6e55647aaa6c1c336bfeb4ba19e661a2fc75f`.

The first controls retained 26 passing existing tests and five failures at the
then-missing rollover entrypoint. Original bridge/control bytes and log
`a324cd66a8dde25bd68e29009919b1e551e210898684013deed6e859a7e009cc`
were recorded in #623 before implementation. No original assertion was removed.
Later controls add malformed/duplicate/exponent counter rejection and a clean
stopped mic with genuinely held context cleanup. Four 30-second synthetic
segments verify exactly 1,920,000 captured samples through one microphone
acquisition, without extending the per-segment capacity or silently dropping
input. Rollover also wakes the original local wait and retains the replacement
notification without polling. Fault, stale identity and pending packet cases
preserve their original state and refuse.

The committed cross-language fixture comes from the actual trusted bridge via
`node apps/kinaiya_flutter/web/export_incremental_pcm_rollover_fixture.js`.
Dart checks its exact receipt and independent retained-tail PCM byte vector.
The previous host/native-wire 206-per-interpreter qualification remains recorded
at its own frozen source; this local change does not claim complete Call
integration, native multi-utterance behavior or representative timing results.
