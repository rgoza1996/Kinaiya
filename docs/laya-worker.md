# Verified local Laya worker

This opt-in worker loads a local checkpoint in a separate process. Artifact
identity does not promote a checkpoint, commit speech or authorize tools.
See [ADR 0021](adr/0021-laya-decisions.md) and [v4 evidence](../evaluation/evidence/laya-2026-10-27/report-v4.json).

## Separate model environment

Keep Torch, Transformers and Laya outside the ordinary Kinaiya host environment.
Provision Laya's `serve` extra from source commit
`b9fe351ce81e2d974bca2bf219c1e4030c0a05ea` (0.4.16), for example in an environment
with the intended Torch build:

```bash
python -m pip install 'laya[serve] @ git+https://github.com/NandhaKishorM/laya.git@b9fe351ce81e2d974bca2bf219c1e4030c0a05ea'
```

Use the exact CPU/ROCm dependency versions in [report.json](../evaluation/evidence/laya-2026-10-27/report.json)
for reproduction. The worker checks package version; the operator provisions
the pinned source/dependencies. It installs or downloads nothing. The existing
clean CPU and ROCm environments were exercised; a clean GPU installation recipe
has not been verified. ROCm uses Torch's `cuda` device spelling.

## Freeze the artifact

The JSON manifest requires a lowercase `model_alias` and exactly five `files`
entries with `path`, positive integer `size_bytes` and lowercase hex `sha256`:

- `model.safetensors` (at most 2 GiB)
- `rl_agent_config.json`
- `encoder/config.json`
- `tokenizer/tokenizer.json`
- `tokenizer/tokenizer_config.json`

Each JSON inference file is bounded to 16 MiB; the manifest to 64 KiB. Duplicate
keys/entries, traversal, symlinks in any path component, special files, incorrect
sizes and digest mismatches fail before loading. Linux descriptor-relative
no-follow opens are required, with no weaker fallback. Normalize tokenizer
metadata with the pinned upstream helper **before** freezing. Load-time changes
to these files are rejected.

The measured [candidate-v4-manifest.json](../evaluation/evidence/laya-2026-10-27/candidate-v4-manifest.json)
has SHA-256 `5363e1728bf8026ca0a9a248d9e2c3b125f3de08888ba4b9c133531260798339`.
The 843 MB model weights are local research artifacts, absent from Git.
Synthetic training data/settings are provided; other runtime/hardware builds
need not produce identical bytes. The manifest pin must come from trusted owner
configuration. Accepting a replacement manifest's self-reported hash removes
the intended identity check. Identity alone does not establish model quality.

## Start and connect

Set `KINAIYA_LAYA_API_KEY` to a separate service secret in worker and host
environments. Never reuse a Kinaiya owner/role token. From the Kinaiya checkout,
use the separately provisioned model Python:

```bash
python -m services.decisions.laya_worker \
  --model-dir /absolute/path/to/candidate-v4 \
  --manifest /absolute/path/to/candidate-v4-manifest.json \
  --expected-artifact-sha256 5363e1728bf8026ca0a9a248d9e2c3b125f3de08888ba4b9c133531260798339 \
  --device cpu --port 8871 --threads 4 --concurrency 2
```

Use `--device cuda` for the measured ROCm environment. The worker binds only
`127.0.0.1`, keeps one checkpoint resident, caps token overrides at 1,024,
disables idle unloading and requires the manifest alias on both inference
routes. Inherited `LAYA_*` settings and Hub tokens are removed; Hub access is
offline. Automatic language/task routing cannot load another checkpoint.

Files are streamed into an owned private snapshot and loaded only there.
Replacing the source after verification cannot change the loaded bytes. Normal
shutdown/loading failure removes the snapshot; abrupt process termination can
leave a private temporary directory, which is never reused automatically. The
source directory is never deleted or rewritten.

Configure only the Decisions role alongside separate Agent/Converse profiles:

```json
{
  "decisions_window_chars": 4000,
  "providers": {
    "decisions": {
      "kind": "laya",
      "base_url": "http://127.0.0.1:8871",
      "model": "kinaiya-v4",
      "api_key_env": "KINAIYA_LAYA_API_KEY",
      "expected_artifact_sha256": "5363e1728bf8026ca0a9a248d9e2c3b125f3de08888ba4b9c133531260798339",
      "allow_cloud": false,
      "timeout": 5
    }
  }
}
```

Authenticated successful responses carry `X-Kinaiya-Laya-Artifact-SHA256`.
The adapter checks it before reading **every** health/inference response body.
Missing/different identity refuses classification without a heuristic/cloud
fallback. Readiness also requires actual residency/device. Local artifacts keep
`revision: null` and report `artifact_sha256`. Unpinned legacy services retain
the original reported-revision readiness check. This authenticated worker
receipt is not hardware attestation against a malicious endpoint. The resource
coordinator does not own a standalone process started by this command. Optional
[managed residency and AMD handoffs](adr/0022-managed-laya-resources.md) launch
their own verified child instead. Authenticated health also reports a worker
PID receipt; it does not independently establish process ownership.

Run the [native probe](../scripts/probe_laya_native.py) with the same pin:

```bash
python scripts/probe_laya_native.py --url http://127.0.0.1:8871 --model kinaiya-v4 \
  --expected-artifact-sha256 5363e1728bf8026ca0a9a248d9e2c3b125f3de08888ba4b9c133531260798339 \
  --output .dev-run/pinned-laya-probe --context-profile preceding_converse
```

The output directory must be new. Laya, native permissions, journal and file
tools are real; text and Agent/Converse inference are synthetic fixtures. This
does not establish microphone, acoustic interruption or paid-provider evidence.
