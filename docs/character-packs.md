# Local character packs

The Flutter web client's **Appearance** control is available in Converse and Agent Chat. **Import character pack** opens a local JSON file, validates it, and displays the real artwork with its name, author, version and license. **Use character pack** explicitly activates that preview. Cancel leaves the current character unchanged; **Restore default character** returns to the original Kinaiya pack. No Agent, model call or server restart is required.

**Export current pack** downloads `kinaiya-character-pack.json`. It includes the pack's source artwork, sprite sheets and provenance text. It excludes conversations, credentials, permissions, personality and local color/motion preferences. The browser decides the download location; an application download request does not establish that the user retained a file.

## Package format

The first supported package is UTF-8 JSON with these exact top-level keys:

```json
{
  "package_schema": 1,
  "manifest": {"pack_schema": 2},
  "files": [{"path": "assets/example/sheet.png", "base64": "..."}]
}
```

This example abbreviates the required v2 manifest and file data. Export the actual default pack for a complete working example. The [bundled manifest](../apps/kinaiya_flutter/assets/character/pack.json) declares pack/version IDs, name, author, license, provenance source asset and SHA-256, generator and prompt file, theme colors, an explicit null voice profile, fallback animation, anchor/scale limits, sheet geometry/checksums and animation frames/timing. Core animations are idle, listening, thinking and speaking. Unknown activity semantics use the validated fallback.

Each declared sheet, source PNG and provenance text file must appear exactly once in `files`. No unrelated files are accepted. Paths are logical keys under `assets/`, resolved only inside the imported memory bundle. They are never filesystem paths or URLs. Base64 is canonical; image checksums must match the manifest. Modifying an image requires updating its checksum and geometry. A palette-only change requires no image regeneration. Rights/provenance fields are supplied by the pack author; validation does not establish ownership or license authenticity.

Imports accept v2 only; existing bundled v1 compatibility remains. ZIP is an optional roadmap packaging convenience and is not implemented here. Remote images, scripts, hooks, voice/personality/permission fields and provider configuration are not imported.

## Bounds and failure behavior

The importer bounds raw JSON to 12 MiB, manifest and provenance text to 64 KiB each, files to 12, and total decoded file bytes to 8 MiB. PNG texture edges are at most 4096 pixels, aggregate image area at most 24 Mi pixels, with the existing sprite-sheet budget of 16 Mi pixels. Existing sheet/frame/count/timing limits still apply. Duplicate JSON keys (including escaped equivalents), duplicate paths, excessive nesting, unknown fields, malformed data, invalid PNG structure/CRC, APNG, out-of-bounds frames and corrupt decoding are rejected. Dimensions and allocation budgets are checked before engine decoding. Codecs/images are disposed after inspection; renderer loads retain their own ownership checks.

An invalid import stays inactive with a visible error. A rendering failure uses the original artwork and a visible fallback message. Pack and bundle activation is atomic, and an older asynchronous result cannot replace a newer owner choice. None of these checks makes an image decoder infallible; supported platform/device hardening remains part of production acceptance.

## Preferences and storage

Colors follow explicit owner overrides, selected pack colors when enabled, then Kinaiya's exact default `#141091` / `#FFFFFF`. Light/dark foregrounds and containers adapt for readability. **Clear color overrides** restores the selected pack/default precedence. **Reduce motion** freezes decorative animation and skips transitions; the device's reduced-motion setting also applies. **Show character** can be turned off without removing transcript or task controls. Captions remain available and the non-character fallback always retains text.

Web preferences and an optional imported package use one transactional `kinaiya-presentation-v1` IndexedDB snapshot for that browser origin. Restored data is validated again. Writes are serialized, and late restores cannot overwrite newer actions. Quota/denial errors preserve session state and remain visible alongside action results. Storage is not encrypted, synchronized across devices or guaranteed to survive browser data clearing. Cross-tab coordination is not claimed. Backend credentials and conversation/task data are never included.

The native client currently uses session-only presentation state and explicitly labels unsupported import/export/persistence. This does not establish native/mobile calling or complete M5/M10 acceptance.
