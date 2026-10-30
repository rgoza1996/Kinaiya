# Kinaiya default pack: seated idle v1 provenance

The owner requested that the animated mascot sit on the water when idle, like
its approved moonlit concept. This is a development asset edit using the built-in
OpenAI image tool, not a runtime provider or model capability. The original owner
source identity remains unchanged at `assets/mascot/kinaiya-mascot.png`, SHA-256
`426188e15d0f8dad7216b51da6a2dddbb7ff74c1aec714a7de01b5b3b3bc6977`.
Existing project distribution/rights metadata is retained; this record is not a
separate license authenticity guarantee.

## Selected seated atlas

- Asset: `assets/character/kinaiya-seated-idle-v1.png`.
- SHA-256: `1322c7fa9ba76ad0cbc6ac8409564d5247d0366bf53fe368f9a39338ac938259`.
- Encoded size: 1,262,705 bytes; actual RGBA PNG dimensions: 1254 by 1254.
- Actual frame rectangles: 627 by 576, two columns and two rows; 102 trailing
  bottom pixels are unused. This is measured geometry, not the requested grid.
- Only registered left-column frames 0 and 2 are selected. The right column
  has approximately 40px horizontal body displacement and is not played.
- `idle`: `[0,0,0,0,0,2,0,0]`, 280ms per frame, looping.
- `idle_blink`: `[0,2,0]`, 220ms per frame, looping. Both begin with frame 0,
  so reduced motion uses the seated open-eye pose.

The selected PNG is copied without pixel changes. The generated exact square
cell grid, generous margins and perfect registration were not achieved. The
curated two-frame selection preserves a small blink without the rejected body
jump. No renderer/validator weakening, image-file editing (pixel shifting,
cropping or resampling), or regeneration of existing standing/activity atlases
was performed. Existing
non-idle animations keep their original assets, indices, timing and behavior.
The original static standing artwork remains the truthful loading/decoder-failure
fallback; this change does not promise seated art before successful decode.

## Exact generation record

The owner retained both prompts and output identities, plus read-only alpha
inspection. The pose reference is an owner-supplied image; its bytes are not
included in exported packs. The established core atlas is retained unchanged.

```json
{
  "tool": "OpenAI built-in image_gen",
  "date": "2026-10-29",
  "reference_1": {
    "role": "seated pose reference: owner-supplied moonlit illustration",
    "sha256": "73f0502fb6b93a324e90b82e35799bfafd318cda6ce380ecc8647add54d1af9e"
  },
  "reference_2": {
    "role": "existing mascot style and identity reference",
    "asset": "assets/character/kinaiya-core-v1.png",
    "sha256": "4eabec3223bc1a921645ce086a1f9fbf8b0098e66c1017e73af826197ff39412"
  },
  "first_output_sha256": "07d2d1887860e5622af9f9139915c93f69019ee20ae91aea5fb52d0dd9eba7f7",
  "selected_output_sha256": "1322c7fa9ba76ad0cbc6ac8409564d5247d0366bf53fe368f9a39338ac938259",
  "first_prompt": "Use case: identity-preserve. Create one production transparent PNG sprite atlas for Kinaiya's SEATED IDLE animation. Reference 1 supplies the exact desired seated-on-water pose: goddess sits with one knee raised, the other leg elegantly folded, one hand resting by her side on the water, long flowing hair, crescent and four-point star in her hair, white draped dress. Reference 2 supplies the established flat indigo #141091 and opaque white mascot style. Do NOT use the standing pose from reference 2. Preserve Kinaiya's identity, elegant modest dress, anatomy and head proportions. Output exactly 2 columns by 2 rows of equal square cells, four distinct frames, square canvas. All four frames show the SAME SEATED pose on small concentric water-contact ripples. Subtle idle loop: frame1 relaxed open eyes, frame2 gentle breathing/hair drift, frame3 soft blink closed eyelids, frame4 return toward relaxed with slightly different hair drift. Keep the seated body, head, raised knee, hands and contact ripple registered to the same position and scale in all four cells; full figure/hair/ripple inside each cell, generous transparent margin. Contact ripples align at 85 percent of cell height, seated silhouette uses about 75 percent of cell width and 65 percent of cell height. Genuine alpha transparency outside the figure and tiny ripple; dress and skin interiors remain opaque white. Crisp vector-like flat shapes in deep indigo and white only; clean antialiased edges. No scenery, moon, stars outside hair, shadows, gradients, floating standing figure, text, grid lines, gutters printed, labels, watermark or UI. This must be four real animation frames with visible gentle changes, not four duplicate stills.",
  "refinement_prompt": "Use case: precise-object-edit. Edit this exact 2x2 seated Kinaiya animation atlas. Preserve the same four SEATED poses, blink ordering, modest white dress, indigo/white palette, face/hair identity, transparent background and four equal square grid cells. Fix ONLY registration, safe margins and stray pixels. Each of the four characters must use the SAME SCALE and SAME seated body/knee/head/ripple center position in its cell; small hair and eyelid changes only. Align the water-contact ripples to EXACTLY 85 percent of every cell height; do not let lower-row figures sit higher than upper-row figures. Shrink all four figures equally so the full seated figure plus ripple fits within 75 percent of cell width and 72 percent of cell height, leaving at least 9 percent fully empty transparent margin on LEFT AND RIGHT of EVERY cell, and clear empty margins on top/bottom. No ripple may touch or cross a cell border. Remove all stray detached blue speckle noise outside the intentional clean hair/body/ripple silhouette. Clean crisp antialiased flat vector-like edges; no shadows or texture. Genuinely transparent alpha outside figures and small water ripples, opaque white skin/dress interiors. Keep exactly2 columns by2 rows, no lines, labels, text, watermark, scene or background. Preserve a visible gentle hair-drift/blink idle animation, with stable water contact and no body jumping.",
  "selection": "Copy selected PNG unchanged; declare actual 1254x1254 texture and 627x576 frame rectangles. Rows use measured 576 pixel pitch, leaving 102 bottom pixels unused. Use only registered left-column frames 0 and 2. The right-column frames have about40pixels horizontal displacement and are not selected. No pixel edits, remapping or validator relaxation. The requested exact square grid and generous margins were not fully honored; provenance reports actual geometry instead.",
  "read_only_inspection": {
    "mode": "RGBA",
    "alpha_extrema": [
      0,
      255
    ],
    "opaque_threshold": 200,
    "bounds_by_actual_cell": [
      [
        52,
        98,
        624,
        557
      ],
      [
        12,
        98,
        584,
        557
      ],
      [
        48,
        93,
        624,
        556
      ],
      [
        15,
        93,
        583,
        558
      ]
    ]
  }
}
```

## Historical records

The following original records are copied verbatim so the single provenance file
inside an exported package remains self-contained. The original source files
[core record](kinaiya-core-v1.prompts.md) and
[activity record](kinaiya-activity-v1.prompts.md) remain unchanged.

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


---

# Kinaiya semantic activity atlas v1

Generated with the built-in OpenAI image tool on 2026-10-27. This development
asset is separate from Kinaiya's planned product Meta image-generation workflow.
The approved original mascot and prior atlas remain unchanged. Rights metadata
describes project distribution; it is not a model/provider licensing guarantee.

References:
- Original `assets/mascot/kinaiya-mascot.png`, SHA-256
  `426188e15d0f8dad7216b51da6a2dddbb7ff74c1aec714a7de01b5b3b3bc6977`.
- Prior `assets/character/kinaiya-core-v1.png`, SHA-256
  `4eabec3223bc1a921645ce086a1f9fbf8b0098e66c1017e73af826197ff39412`.
  Its two-pass generation record remains in `kinaiya-core-v1.prompts.md`.

Output `assets/character/kinaiya-activity-v1.png`, SHA-256
`c89deb785e27cb8a741fbcf697b991e46f9b03ab2256342a2c340d555761d4c2`.
Actual size: 1254×1254, RGBA8, 1,592,545 bytes. The four-column/four-row grid
uses 313×313 cells with two unused trailing pixels at right/bottom. No image
postprocessing was applied. PNG chunk CRCs and alpha range (0–255) were checked.
Frame registration and loop continuity are visually reviewed, not certified as
perfect; these generated poses retain small silhouette shifts.

Rows: generic work with a book; waiting for input; important attention; critical
attention. Alert clips are available to semantic inputs; no keyword-based urgency
classifier or new notification authority is created. Specialized work semantics
without a distinct clip explicitly fall back to generic work. The current host
does not supply trustworthy ongoing specific tool phases, so live task animation
uses generic work or blocked. Full Converse emotional direction remains open.

## Exact generation prompt

Use case: identity-preserve. Asset type: production spritesheet for the Kinaiya application. The first reference image is the existing 4x4 atlas and determines exact character identity, visual line style and compact frame composition; second is original approved identity. Create a NEW transparent 4-column x4-row square atlas, exactly16 cells with equal cell size and generous transparent margin, no lines/borders/labels/text. Preserve Kinaiya's indigo #141091 and white limited palette, moon-and-star hair mark, flowing hair, draped white dress, full body and face identity. Each row is one four-frame gentle loop; align character feet and head consistently across its four cells. Row1: working quietly, holding an open book and following its lines with a finger (generic observable work, thoughtful without claiming results). Row2: waiting for user input, friendly attentive open palm, small patient blink and slight arm motion (blocked). Row3: important alert, politely raising one hand and looking toward viewer, subtle motion. Row4: critical alert, clearly raised open stopping palm, attentive focused face, subtle motion, no panic. Different rows clearly readable at small icon size. No extra characters, no props outside the relevant row, no success checkmarks or fake progress, no scenery, no background, real alpha transparency. Use a precise regular 4x4 grid where every pose fits fully inside its own cell. Preserve white areas of skin and clothing as opaque white; only the outside background is transparent.
