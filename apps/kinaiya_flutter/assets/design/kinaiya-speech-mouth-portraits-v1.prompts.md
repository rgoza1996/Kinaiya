# Kinaiya speech mouth portraits v1

- Owner request: replace the unnatural fully open procedural mouth with image-generated mouth poses while preserving the approved Kinaiya identity.
- Generator: built-in image_gen tool, 2026-10-30. No CLI/API fallback used.
- Edit/reference input: `kinaiya-expression-reference.png`, SHA256 `ca82097c4401cabfbb290fefb5c4387a918941003afc98315114c18992f3b989`.
- Generated output: `kinaiya-speech-mouth-portraits-v1.png`, SHA256 `d7aedd4fd20d44daac5c81ee43d4ffa70a5f74ba2c9b7f8ae188112004703585`.
- Actual output dimensions: 1881 × 836, RGB PNG. Three equal columns and two rows, 627 × 418 per cell. The requested 2304 × 1024 was not returned; rendering uses the actual dimensions.
- Cell order: closed, half-open, open, rounded, pursed, teeth. The renderer retains the original neutral portrait and samples only generated mouth regions, with fixed scale and per-pose lip anchors. It never swaps the generated full head, eyes, hair or body.
- The original approved atlas and raw generated output are retained. No offline raster editing, cropping or resizing was applied to the generated PNG; the application samples sprite regions at render time.
- These are visual mouth poses, not evidence of provider phoneme timing.

## Exact prompt

Use case: identity-preserve.
Asset type: production anime character mouth-pose portrait sprite atlas for the Kinaiya application's Converse page.
Edit/reference image: the supplied approved Kinaiya expression atlas. Use ONLY the upper-left neutral portrait as the exact identity, pose, framing, lighting and art-style reference. Preserve her mature gentle anime face, three-quarter turn to the viewer's right, indigo hair, white crescent moon hair ornament and star earring, blue eyes, white clothing, white-and-indigo illustration style and blue backdrop.
Primary request: create a carefully aligned six-frame portrait atlas with NATURAL, restrained speech mouth poses. Every frame must reuse the identical head, eyes, hair, body and background; only the lips and tiny nearby lower-face area may change. No head, eye, jawline or hair position drift. The neutral mouth anchor must remain identical in each frame. Use an exact 3-column by 2-row grid of equal 3:2 landscape cells, no gutters, no borders, no labels. Target sheet 2304 by 1024 so each cell is 768 by 512.
Reading order: row1 col1 closed relaxed neutral lips; row1 col2 slightly parted conversational mouth; row1 col3 comfortably open speaking vowel, subtle upper teeth, relaxed lip corners. Row2 col1 small rounded 'oh'; row2 col2 smaller lightly pursed 'oo'; row2 col3 subtle upper-teeth/lower-lip contact for F/V.
The largest open mouth should be elegant and anatomically plausible: modest vertical opening, same lip width and asymmetry/perspective as the reference, upper lip gently curved, small slim upper teeth, muted indigo oral cavity, subtle tongue only if natural. A normal speaking expression, not a giant smile, shout, surprised O, yawning or rubbery wedge. Avoid a triangular black hole, oversized teeth, exaggerated grin, sharp angular mouth, extra facial marks, realism, new character design, blush, text, watermark or UI. Keep the mouth drawn as naturally integrated original illustration, not a graphic pasted on the face.
