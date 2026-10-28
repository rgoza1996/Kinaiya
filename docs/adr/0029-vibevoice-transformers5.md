# ADR 0029: Explicit VibeVoice Transformers 5 runtime derivative

Status: implemented experimental CPU derivative for [#69](https://github.com/rgoza1996/Kinaiya/issues/69) and [#70](https://github.com/rgoza1996/Kinaiya/issues/70), with independent final review and actual HTTP/resource/STT checks completed; updated GitHub CI is pending. This does not close full M2, change a running profile, or promote a default. The original 4.51.3 profile and its measurements remain available in Git history and [ADR 0028](0028-retained-vibevoice.md).

The original Microsoft runtime cannot import unchanged with Transformers 5.21.0. Its acoustic-tokenizer registration collides with Transformers' native model, its fast Qwen tokenizer module was removed, and the cache/generation/loading APIs changed. Runtime `kinaiya-vibevoice-runtime==1.0.0+kinaiya.2` carries an explicit [four-file patch](../../packages/providers/vibevoice_transformers5.patch) against `microsoft/VibeVoice@16fb2cb1217c9934a886e1948ffb06120caa2df5`.

The [manifest](../../packages/providers/vibevoice_manifest.json) retains all 22 original source records, including Git blob provenance, alongside all 22 installed derived-source SHA-256 digests and the patch digest. The builder reads only verified regular source files, checks the patch through no-follow descriptors, applies exact existing-file hunks without executing code or an external patch command, and verifies every final source hash. It refuses fuzzy offsets, overlapping or duplicated hunks/files, renames, new files, traversal, changed context and unexpected output bytes. Deterministic wheel metadata/RECORD, MIT notice, exclusive outputs and original/derived provenance remain enforced. This is a source compatibility derivative; it is no longer described as byte-preserving packaging alone.

The patch changes the retained streaming path:

- Construct Microsoft's acoustic tokenizer directly, leaving Transformers' native acoustic `AutoModel` registration intact. The retained model does not fall back to a different acoustic implementation.
- Use Transformers 5's tokenizers-backed `Qwen2Tokenizer`. The existing VibeVoice fast-tokenizer symbol remains available, with the same verified local tokenizer files and speech token IDs.
- Use native `DynamicCache` layers. Remove the old mock layers, catch-all cache compatibility fallback and mutation of Transformers' global parallel-style registry. Preserve VibeVoice's explicit window `cache_position` updates for both positive and negative conditioning.
- Follow the updated generation-configuration signature and inherit the standard weight-tying implementation. The removed custom override had no output head to tie in this fixed profile and did not accept the new loading keywords.
- Allocate the diffusion scheduler's real CPU constants explicitly on CPU during meta-device model construction. Model checkpoint tensors still use the normal safetensors loading path.

The optional hash lock selects Torch `2.14.1+cpu`, Transformers `5.21.0`, Pillow `13.0.0` and setuptools `84.2.1`. Hugging Face Hub is pinned to `1.33.0`: Diffusers 0.39 requires Hub below 2, so an unconstrained Transformers-only upgrade produced an incompatible environment. The final 80-package installed environment passes `uv pip check`. No upstream demo UI/server dependencies are installed.

One reproducible setup route, using separate owner-controlled absolute paths, is:

```bash
uv venv --python python3.12 /absolute/owner/vibevoice-env
uv pip sync --python /absolute/owner/vibevoice-env/bin/python --require-hashes requirements-vibevoice.lock
python scripts/build_vibevoice_runtime.py \
  --source-dir /absolute/owner/pinned-runtime-source \
  --license-file /absolute/owner/assets/licenses/runtime-MIT.txt \
  --output-dir /absolute/owner/wheels
uv pip install --python /absolute/owner/vibevoice-env/bin/python --no-deps \
  /absolute/owner/wheels/kinaiya_vibevoice_runtime-1.0.0+kinaiya.2-py3-none-any.whl
uv pip check --python /absolute/owner/vibevoice-env/bin/python
```

The source directory must contain the original pinned `vibevoice/` tree. Supply the original runtime MIT license recorded by the manifest. The builder itself applies the checked patch; passing already-patched source fails original-source verification. The built wheel observed in this evaluation has SHA-256 `b7936a32d5df6b6856ef55395dc225402576b802182761a7d16836bd3381b352`. Wheel installation is an explicit setup action; inference requests never install packages or select alternate patches, classes, voices or settings.

The existing backend now requires the derivative distribution/version and Transformers 5.21.0 before import, checks every installed Python file against derived hashes, and rejects alternate import origins. All checkpoint/tokenizer/preset hashes, offline flags, safetensors-only weights, fixed public Carter voice, CPU float32/SDPA, sample/context limits, bounded worker transport and request-owned cancellation remain. The restricted preset decoder still uses `weights_only=True`, inert allowlisted decoding, complete fixed-schema/tensor validation and explicit native cache reconstruction. It does not unpickle a Transformers class or dispatch methods named by the asset. Owner protection against concurrent file mutation is still necessary; offline flags are not an OS sandbox.

[Recorded evidence](../../evaluation/evidence/vibevoice-transformers5-port-2026-10-28.json) includes the exact wheel provenance, lock/manifest digests, installed dependency audit, tokenizer parity and actual guarded installed-wheel requests. Across Transformers 4.51.3 and 5.21.0, the complete 151,665-token vocabulary, special IDs and 30 public Unicode/punctuation sample token sequences were identical. Focused backend/builder tests passed 97 cases, including layer-only cache reconstruction and malformed/tampered patch rejection; changed Python files pass Ruff checks.

Actual guarded loading, including asset hashing, took 6.999 seconds. One public synthetic request emitted 26 native 24 kHz chunks in 9.491 seconds. A separate request cancelled from the first callback returned from native production 0.421 seconds after that callback (0.636 seconds total request duration); a fresh request then emitted 29 chunks in 9.569 seconds. These few uncontrolled-host observations prove compatibility and recovery through the direct backend API. They do not measure HTTP disconnect latency, producer-thread exit, physical audio, kernel preemption, p95, speech quality or sustained realtime playback. Earlier prototype runs are preserved separately and do not replace this guarded installed-wheel proof.

The installed audit reported zero advisory records among checked packages. It explicitly skipped the locally built derivative distribution and exact `torch==2.14.1+cpu` wheel. Official PyPI metadata for base Torch 2.14.1 listed zero advisories at lookup time; that metadata is not a binary audit or reachability proof for the CPU wheel. Source review, compatibility checks and renewed lifecycle evidence are required alongside dependency metadata. Model-card research-use qualifications and the absence of independent preset voice-rights evidence from ADR 0028 remain applicable.


## Integrated worker and resource verification

At lead implementation `9349050`, a separate new-process request and nine warm public synthetic replies passed through the real authenticated HTTP worker using the installed `.2` wheel. All nine warm replies emitted PCM while the native producer was still active. Filesystem caches were not flushed; host load was uncontrolled; measurements completed before the lead regression suite began. Diffusion output lengths differ, so this is not a controlled comparison with earlier environments.

| Measurement | Observed result |
| --- | --- |
| Warm first decoded PCM | 0.226 s median; 0.216–0.278 s range |
| Warm first non-silent frame, 1% PCM amplitude threshold | 2.877 s median; 1.482–3.360 s range |
| Warm full synthesis RTF including HTTP | 2.604 median; 2.394–2.843 range |
| New-process first decoded/non-silent PCM | 7.500 s / 10.111 s |
| Observed process-lifetime peak RSS | 6,662,955,008 bytes |
| HTTP close to observed producer-thread exit | 0.464 s, one cancellation observation |

Cancellation confirmed producer termination without quarantine; subsequent synthesis succeeded. A separate real resource proof reaped the original owned process group, restored a new PID, generated fresh 24 kHz PCM, rejected old speech with HTTP 409 before and after restoration, and preserved durable owner-off. The probe used an explicit 8 GiB restoration reserve. Private temporary data and every owned worker were removed. Control/text inputs were synthetic; this is not a real Agent/cloud task or GPU release proof.

Whisper tiny.en in the same environment transcribed one synthetic WAV as “I am here. What would you like to do?” through the HTTP endpoint in 0.937 seconds. No microphone recording or human listening was performed.

The integrated Python 3.12 suite passed 1,217 tests in 185.73 seconds with one Starlette deprecation warning. A separate Python 3.11 run passed 264 focused backend, builder, worker and Converse cancellation tests. Ruff lint and formatting passed; the exact source patch retains upstream whitespace as data. The independent final Codex review at `5d5bfbb` passed 188 focused tests, verified source/wheel/tensor/tokenizer identities and reported no actionable findings. A lead rerun of the installed-environment audit reported zero listed advisories for 78 covered packages, with the local derivative and direct CPU Torch binary explicitly skipped. This is not general source/binary safety proof.

Updated cross-version CI remains required before integration. Full M2 stays open: this CPU profile is slower than realtime, first non-silent output misses the warm-onset target, and physical audio, listening, p95, loaded contention and Ming 3 Tiny acceptance are unverified. The running reference/default providers remain unchanged.
