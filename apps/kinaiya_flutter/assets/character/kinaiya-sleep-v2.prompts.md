# Revised Home sleeping pose

The owner supplied a side-lying pose reference and requested Kinaiya resting
her head on her folded arms, with no pillow. The built-in image generation tool
was used on 2026-11-01 with that pose reference and the existing
`kinaiya-seated-idle-v2.png` as the character design reference. The source
photograph is not included in the application.

`kinaiya-sleeping-idle-v2.png` is a 1536×1024 transparent, four-frame sheet
(SHA-256 `068d98efd497601139b1ec976f1a4b691b0e28d885ab0592cccb4ae57a83a698`).
The generated PNG is retained unchanged.

Prompt:

> Create a 2 by 2 production sprite atlas for an illustrated app, fully clothed Kinaiya sleeping on her side. Image 1 is the pose reference ONLY: body straight on her side, both legs extending together toward the right, head at left; no bed or pillow. Her head rests naturally on her own two arms folded together beneath her cheek. Image 2 is the sole character design and art style reference: preserve Kinaiya's face, long flowing indigo hair with white streaks, white Grecian robes, and her small chest mole if visible; no crescent or star hair ornaments. Draw complete, coherent human anatomy and feet, with her legs resting naturally together and her flowing robe covering her modestly. A small ripple may surround her at the waterline. Four equal landscape cells each show the SAME pose at the SAME size and position, with only subtle breathing. Include transparent padding all around each cell, no overlap across center row or column. Genuine transparent background, opaque white skin and clothing. No pillow, bed, sky, moon, decorations, text, grid or extra people.

`kinaiya-sleep-transition-v2.png` is a 1536×1024 transparent atlas
(SHA-256 `cb26c777b9677e0021e9dda3c983b3e08cf8f07b8a6ef2fcdba5cf1eca4a188b`).
It was edited from `kinaiya-sleep-transition-v1.png`
with the new sleeping sprite as a reference, then edited for frame spacing. The
application uses the new sleeping sprite for the final transition frame so that
it exactly matches the idle pose.

Transition prompt:

> Edit image 1, the 2×2 Kinaiya sleep/wake animation atlas, to transition from the existing seated pose in its upper-left frame to the new sleeping pose in image 2. Maintain four equal landscape cells in reading order: upper left seated awake exactly like image 1; upper right leaning down on her side; lower left nearly lying sideways with both legs extended toward the right; lower right asleep on her side, head resting on her own folded arms, legs together stretched to the right exactly like image 2. No pillow. Modest flowing white robes, coherent legs and feet with no extra limbs; same face, naturally proportioned head, long indigo hair with narrow white streaks, no hair ornaments. Each entire character and any ripples must fit in its cell with generous transparent padding and no crossing the horizontal or vertical midlines. Keep all poses centered, similarly sized and sharing the same waterline. Genuine transparent background, opaque white skin and robe; no moon, stars, sky, checkerboard, grid lines, text or other people.

Final pose edit:

> Use case: precise-object-edit. Image 1 is a 2x2 animated sprite atlas. Keep the top-left, top-right and bottom-left frames unchanged. Replace only the BOTTOM-RIGHT frame with the sleeping character pose shown in image 2: she lies on her side with both legs extended straight together toward the right, her head resting on her own folded arms. Match the reference silhouette, not a curled or bent-knee pose. Preserve Kinaiya's indigo hair with white streaks, modest flowing white robe, face and deep blue-and-white palette. Keep the entire pose centered within the bottom-right cell with transparent padding and no overlap of grid boundaries. Preserve the genuine transparent background, opaque skin and clothing. No bed, pillow, stars, text, or extra people.

Frame spacing:

> Make a clean 2×2 transparent sprite atlas using this image as reference. Put each character in the middle of its own quadrant, with the complete character and ripples at only 65% of the width and height of that quadrant. Leave a large empty transparent margin around each of the four complete poses, especially between the upper and lower rows. Match the four poses exactly, including bottom-right lying on her side with her legs straight together and her head resting on folded arms. Keep her white robe, indigo hair, white hair streaks, colors and expression. No background, checkerboard, lettering, UI, extra figures or ornaments.
