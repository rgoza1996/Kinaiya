# Home sleeping sprites

Both PNGs were made with OpenAI's built-in image generation tool on 2026-11-01.
The transparent sheets are separate from the existing responsive Home scene.

`kinaiya-sleeping-idle-v1.png` is a 1536×1024, 2×2 sheet, generated using
`../design/kinaiya-sleeping-home-v2.png` and `kinaiya-seated-idle-v2.png`.
SHA-256: `030fbcf046acd0087cb7b6fa967c9f4757f89b1eb8aeb6669ea04b7a2a826276`.
Prompt: Create four equal, transparent frames of the same Kinaiya sleeping
on her side on water, head resting on her arms, eyes closed, long hair with
slender white streaks, no head ornaments, natural head proportions and white
opaque skin and robe. Keep all poses aligned, varying only gentle breathing.
Exclude the sky, moon, background, text, grid and UI.

`kinaiya-sleep-transition-v1.png` is a 1536×1024, 2×2 sheet made from the
seated and sleeping references, then edited to improve leg silhouettes and
keep every pose within its own cell. Prompt: Show the same fully clothed Kinaiya
sitting, leaning, reclining and sleeping on the water, in four aligned frames;
correct the legs, keep the long robe, hair, face and style consistent, with
transparent space between cells and no scenery or UI. The transition also plays
backward to show her waking. The final file is the unmodified generated PNG.

## Generation prompts

Sleeping idle:

> Use case: stylized-concept. Create a production 2 by 2 animation sprite sheet with genuine transparent background, each of the four equal cells containing one complete, horizontally reclining sleeping Kinaiya, matching the person and pose in image 1 and the deep indigo-and-white character style and slender white hair streaks in image 2. She lies peacefully on her side on water, eyes closed, head resting on her arms, a little submerged at the lower edge; show her whole body and long hair. No crescent or star ornaments on her head. Her head must remain naturally proportioned and not enlarged at the back. All four frames MUST have the exact same position, dimensions, alignment, identical pose, hair silhouette, hands, face and feet; only extremely subtle breathing of the upper torso changes gradually between frames. Each frame must fit entirely within its own equal cell with clear transparent padding. Do not include the moon, sky, full scene, background, decorative stars, text, checkerboard, grid lines, borders, UI or extra people. The sprite will be composited onto the existing water background. Keep white skin and white clothing fully opaque. Transparent alpha around every frame.

Transition:

> Create one transparent 2 by 2 production animation sprite atlas of the SAME Kinaiya in a smooth transition from seated to sleeping on water. Reference image 1 is her awake seated 2x2 sheet; reference image 2 is her sleeping 2x2 sheet. Four equal 768 by 512 landscape cells in strict reading order, each containing ONE entire character. Top-left: seated awake precisely like image 1. Top-right: Kinaiya gently leaning and lowering her upper body, eyes relaxed. Bottom-left: nearly lying down, supporting her head with arms. Bottom-right: lying asleep precisely like image 2. All frames are centered in their own cells at the same size, on the same waterline and with generous clear padding; no body part may overlap another cell. Indigo and white illustration, natural proportionate head, long hair with white streaks, no ornaments on head. Transparent alpha throughout empty areas; white skin and clothing must stay opaque. No sky, stars, moon, checkerboard, grid, labels, UI, borders or extra characters. The atlas will also play in reverse for waking up.

Leg repair:

> Edit the supplied four-frame illustrated animation atlas for the Kinaiya app. Keep the same four poses, face, hair, colors, opaque white clothing and transparent background. Repair the lower-body silhouettes in each frame: each pose should have two natural, clearly distinguishable legs and feet, covered by the existing long flowing robe, with no extra or merged limbs. Keep the illustration modest and fully clothed. Each whole pose and its water ripple must fit fully within its own equal cell with transparent padding, with no artwork across the middle dividing lines. The four cells show sitting, leaning down, reclining, and asleep. No extra people, background scenery, text, or decorations.

Frame-spacing edit:

> Edit this 2x2 animation sheet to fix its layout for an app. Preserve all four individual characters and their poses and details. Shrink every complete pose including its ripples uniformly to fit with very generous transparent padding INSIDE its own cell. CRITICAL: image is 1536×1024. The top-left pose must fit entirely between x=80..688, y=50..462. Top-right x=848..1456, y=50..462. Bottom-left x=80..688, y=562..974. Bottom-right x=848..1456, y=562..974. The central 100-pixel-wide horizontal and vertical gutters must be entirely transparent, with no overlapping or clipping. Keep each pose modest and fully clothed. Keep crisp indigo and white illustration, genuine alpha transparency in empty areas, no visible grid, labels or background.
