# Optional task media launcher

The Agent launcher can install the native task media owner with an explicit
`task_media` configuration. An absent section keeps the existing launcher,
artifact store and tool defaults. Constructing the owner starts no inference,
capture, decoder or service. It does not establish media/provider readiness.

```json
{
  "task_media": {
    "kind": "meta_video_v1",
    "owner_plan": "/absolute/private/owner/task-media-plan.json",
    "api_key_env": "KINAIYA_META_VIDEO_KEY",
    "video_profile": {
      "source_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "width": 1280,
      "height": 720,
      "fps_num": 30000,
      "fps_den": 1001,
      "frame_count": 1273,
      "duration_seconds": 42.631837
    }
  }
}
```

The SHA above is a synthetic placeholder. The owner must supply the actual
source digest and measured video profile. The profile admits at most1280×720,
1800 frames and60 seconds. Frame-rate numerator is a positive native integer
at most120,000, denominator at most10,000, and their ratio is between1 and60.
Booleans, nonfinite values, extra fields,
inline credentials and arbitrary endpoints are refused. Credentials resolve
only from the named process environment reference, never from generated code.

The plan path is normalized, absolute, free of symlink aliases and outside the
worker workspace. It may not exist until the isolated clip driver captures the
actual complete WAV receipt. Native enrollment then performs the protected-file,
voice-source, Decisions-route, submit, model and source checks described in
[native task media enrollment](agent-task-media.md). The config section cannot
replace that grant or create a model-selected prompt or target.

The launcher creates one protected `NativeMediaAssets` registry at
`<data-dir>/media-assets.sqlite` and passes the same instance to `ArtifactStore`
and the trusted `SamVideoProjector`. Rendering uses `<data-dir>/sam-render` and
the exact native source profile. A trusted callback checks its source SHA before
every original video HTTP dispatch, then the existing exact-source provider and
budget guard remain authoritative. A profile mismatch never becomes a request.

`TaskMediaFactory` owns task-specific grants. Its bound `enroll` hook runs after
confirmed submit and before the sole worker launch. Neither the helper nor the
launcher sends a request while configuring these objects. No fallback, provider
retry or increased spending allowance is introduced.

On shutdown, runtime workers retire first, followed by the original provider
client and shared media registry. An unconfirmed provider close propagates its
original exception and leaves the registry retained for the original projection.
The helper never replaces another injected native owner.

The config tests use fake components and synthetic values only. They do not
qualify decoding, rendering, provider execution, an actual private video or the
live demo.
