# ADR 0022: Managed Laya residency and AMD GPU handoff

Status: experimental implementation for [#53](https://github.com/rgoza1996/Kinaiya/issues/53),
[#9](https://github.com/rgoza1996/Kinaiya/issues/9) and Roadmap 14.2–14.4. Extends
[ADR 0013](0013-resource-handoff.md) and [the pinned worker](../laya-worker.md).
Full M6 scheduling, allocator enforcement, constrained-memory/device-loss tests
and physical voice acceptance remain open.

## Trusted configuration and ownership

The optional `resources.laya` configuration adds one first-party Laya subprocess
to the existing resource controller. Its Python, model, manifest and optional
package directory must be owner inputs disjoint from Agent's workspace, both
lexically and after resolving symlinks. Decisions worker source is protected
from Agent workspaces. HTTP actions cannot supply argv, environment, paths or PIDs.

The separate API secret is resolved in the parent and copied under a fixed
child environment name. It cannot overwrite the trusted child Python path and
must differ from all Kinaiya owner/service credentials. Parent environment and
Meta credentials are not inherited. Hub access remains offline.

WorkerManager's sealed bootstrap grant, pidfds and process-group verification
remain the ownership boundary. The worker's authenticated PID header must match
the retained child PID, alongside the artifact pin, selected alias and actual
device. The header is a diagnostic receipt; it cannot independently establish
ownership or authorize a signal. Existing endpoints are never adopted or stopped.

For `cuda`, explicitly configure an AMD PCI bus ID and ROCm device index. The
parent requires amdgpu driver counters and matching `/proc` DRM fdinfo on the
configured card. It checks process starttime around sampling, bounds reads and
enumeration, and deduplicates repeated descriptors by DRM client identity.
Missing/malformed counters and use of an unexpected GPU fail closed. The
normal host imports no Torch/Laya dependencies.

## Configuration

Use the independently configured Decisions profile from [the setup recipe](../laya-worker.md),
including its explicit alias, pin and separate secret environment reference.
Add to the existing owner-held resource settings:

```json
{
  "resources": {
    "enabled": true,
    "voice_python": "/absolute/path/to/voice-python",
    "voice_port": 8765,
    "laya": {
      "python": "/absolute/path/to/model-python",
      "port": 8871,
      "model_dir": "/absolute/path/to/candidate-v4",
      "manifest": "/absolute/path/to/candidate-v4-manifest.json",
      "device": "cuda",
      "gpu_index": 0,
      "pci_bus_id": "0000:03:00.0",
      "host_memory_reserve_bytes": 2147483648,
      "gpu_memory_reserve_bytes": 4294967296,
      "startup_timeout_s": 45
    }
  }
}
```

The example PCI ID is synthetic; select the actual card. `package_path` can
name an explicitly provisioned Laya target directory; the first-party checkout
retains priority on the child Python path. CPU mode omits PCI binding. Omitting
`resources.laya` retains the existing voice-only lifecycle.

## Admission and restoration

The owner can declare host RAM and GPU headroom separately:

```text
/resources profile local_work_priority
/resources handoff 2147483648 task-id --gpu-bytes 1073741824
/resources restore
```

The corresponding owner-only `handoff` action has optional
`required_gpu_bytes`, default zero. GPU requests require managed GPU telemetry.
An impossible request or unavailable initial measurement refuses before
disrupting voice. Conversation-priority and inactive-task refusals remain.

Handoff quiesces Converse and local Decisions inference, stops both owned
workers through retained handles, checks reaping/group release and samples
driver headroom before issuing a lease. The durable policy retains requested
GPU bytes and a conservative restoration reserve: at least the configured
reserve and the highest observed Laya resident footprint plus margin. Host
restoration accounts for both worker reserves and observed process RSS.

Restoration checks host/GPU headroom, starts verified workers and restores
Converse only after readiness. The current resident footprint is credited on
an already-running managed worker, avoiding an unnecessary duplicate reserve.
A resource-mediated owner-off choice remains off. Agent Chat, permissions and approvals remain
independent of Laya/Converse. Local-only policy still blocks cloud providers;
no fallback provider is selected. Uncertain interrupted actions retain the
existing ledger/recovery semantics.

`gpu_memory` and `gpu_release_observation` are driver telemetry. The legacy
process host's `gpu: unknown/unverified` still denotes absence of a general
GPU allocator/containment proof. Global counters race graphics and other jobs;
their difference is not exact causal attribution or a hard reservation. GPU
requests on other backends, general Agent model scheduling and hostile escaped
daemons are not covered by this AMD adapter.

A pre-existing race remains tracked in #9: an acknowledged owner-off request
through the separate legacy Converse endpoint can be overwritten by an already
running resource restoration. Resource-mediated owner actions are serialized;
the legacy entry point still needs unified durable owner intent. This was
reproduced on main with a disposable CPU worker and is not counted as passing
M6 acceptance.

## Evidence

The public live record links exact source hashes, runtime and budget data to
actual independent core services, cached CPU Whisper/Kokoro, pinned GPU Laya,
and real Meta Contributor Agent/Converse inference. It verifies two owned groups
released, a bounded 256 MiB GPU allocation/compute during the lease, direct
permissions/approval/file evidence while Converse is off, automatic task-bound
restoration to new PIDs, fresh STT/TTS and rejected stale speech. The Agent task
is submitted directly; this probe does not establish conversational task
delegation, human microphone/echo behavior or audible latency.

A first live probe stopped before resolving approval because its fixture
omitted a valid optional `objective_revision` argument. No target file was
written. The corrected fixture checks all effect fields and binds any supplied
revision to the native approval revision. The failure is retained. A separate
Muse Contributor helper run reached its four-step cap; the lead corrected two
fixture errors and added parsing/boundary checks before integration.
