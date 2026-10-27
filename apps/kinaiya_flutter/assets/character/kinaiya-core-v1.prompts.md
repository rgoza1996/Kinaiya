# Kinaiya core sprites: generation record

The original owner-supplied mascot remains unchanged at
`assets/mascot/kinaiya-mascot.png` (SHA-256
`426188e15d0f8dad7216b51da6a2dddbb7ff74c1aec714a7de01b5b3b3bc6977`).
Two OpenAI built-in image-generation calls produced the selected derivative on
2026-10-27. This is a development asset workflow, not the planned Meta image
provider integration in the product. No Meta image-generation capability is claimed.
Project Apache-2.0 terms apply; the reference is the project's existing supplied
artwork, not a separately sourced stock illustration.

The requested 2048-square geometry was not honored: both outputs were 1254-square
RGBA PNGs. The selected second output is copied without pixel edits. The manifest
uses sixteen 313-square frame rectangles and leaves the final two texture pixels
on the right and bottom unused. Explicit runtime bounds use the actual dimensions.
The four source frames in each row play forward and back for a simple local loop.
These are approximate body/mouth poses, without phoneme alignment. More poses,
precise registration, work costumes and expression variants remain future work.

Selected asset: `kinaiya-core-v1.png`, SHA-256
`4eabec3223bc1a921645ce086a1f9fbf8b0098e66c1017e73af826197ff39412`.

## First pass prompt

Use case: precise-object-edit. Asset type: production animation spritesheet for the existing Kinaiya mascot. The input is the approved identity reference: a graceful flat indigo-and-white goddess with long flowing hair, crescent and four-point star in her hair, white draped dress, bare feet, small stylized water ripples. Preserve this exact identity and elegant graphic silhouette; do not redesign her or add accessories. Create ONE 2048x2048 PNG atlas on a genuinely transparent background, exactly 4 columns by 4 rows of equal 512x512 cells, sixteen full-body frames, no gutters, labels, text or visible grid. Keep the same drawing scale, crisp flat vector-like edges, white opaque dress/skin interior, deep indigo #141091 shapes and outlines. Every frame centered at x=256 in its cell, soles/ripples aligned near y=460, full hair/body/ripples inside cell with 24px safe margin. White character interior must NOT become transparent. No gradients, shadows, extra colors, scenery or watermarks. Each row is one seamless 4-frame loop, frame 4 eases back into frame 1. Row 1 IDLE: relaxed source pose, visible gentle breathing and flowing hair/hem sway, subtle eyelid change. Row 2 LISTENING: slight attentive head tilt toward the viewer, open attentive eyes, small upper body lean and soft hair movement. Row 3 THINKING: contemplative lowered gaze with one hand near chin, small thoughtful head motion and swaying hair, no props. Row 4 SPEAKING: friendly conversational expression, four subtle open/closed mouth variations and restrained open-hand gesture, hair/hem drift. Frame differences must be visibly legible at 260px displayed size while preserving the same character, body proportions, camera and anchor in all sixteen frames. This is animation data, not a contact sheet illustration: exact uniform cell geometry and stable registration are essential.

## Second pass prompt

Precise cleanup of this Kinaiya 4x4 animation sprite atlas. Keep the same 16 poses, same indigo/white goddess identity, hair crescent/stars, dress and semantic row order. Fix only atlas registration and edge quality: every character and ripple must sit completely within its own equal-size grid cell, with at least 24 pixels of EMPTY TRANSPARENT margin on ALL FOUR sides of each cell; currently some ripples touch the row below. Make all figures about 80% of their current height so they fit comfortably in each cell. Align the feet consistently across all four frames of each row, keep the centered subject scale identical across the entire sheet. Remove ALL stray blue speckles, loose pixels, white halos and texture noise outside the intentional character/hair/ripple silhouette. Crisp flat indigo #141091 and white opaque interior, with clean antialiased edges only. Transparent background is real alpha; keep the white dress and skin opaque. Rebuild on a 2048 by 2048 pixel canvas if possible, exactly 4 columns and 4 rows; no labels, grid, gutters printed or watermark. Preserve clear visible differences between frames and the attentive/thoughtful/speaking expressions. The output must be a clean usable game sprite atlas, not a textured contact sheet.
