# Sleeping sprite: hidden chest mole

The owner clarified that Kinaiya's chest mole is not visible from the sleeping
camera angle. OpenAI's built-in image generation tool edited the v2 sheets on
2026-11-01. Awake seated artwork retains the mole. The resulting transparent
PNGs are stored unmodified; frame registration is handled by the Home renderer.

- `kinaiya-sleeping-idle-v3.png`: SHA-256 `1e4c38af6f789dfb35eeacecdc2ac328c86cb6b2e694840cd7fc76bb4e54e110`.
- `kinaiya-sleep-transition-v3.png`: SHA-256 `dac0b49a10bfa261d03ac49676986cfe70b3c69f85089efc2f800d03a23eecb6`.

Idle prompt:

> Use case: precise-object-edit. Edit this exact 2×2 transparent sprite sheet. In ALL FOUR sleeping poses remove the tiny dark dot mole on the visible chest near the neckline, filling only that dot with the surrounding white skin. At this viewing angle it should not appear. Preserve everything else pixel-aligned: the same face, hair, pose, body, folded arms, robe, water ripples, exact size 1536×1024, cell boundaries, frame placements, transparent alpha and opaque white areas. No new marks, no decorations, no text, no background.

Transition prompt:

> Use case: precise-object-edit. Edit this exact 2×2 transparent sprite sheet. Remove only the tiny chest mole from the BOTTOM TWO reclining or sleeping frames, replacing that dot with surrounding white skin; leave the mole in the two top seated frames where the chest is visible. Preserve everything else pixel-aligned: face, hair, robe, limbs, pose, water ripples, exact 1536×1024 size, cell boundaries, frame placement, genuine transparent alpha and opaque white areas. No new marks, no text, no background.

The transition result crossed frame boundaries, so it was edited once more:

> Fix this 2×2 transparent sprite atlas. Uniformly shrink each entire complete pose including ripples to 65% of each quadrant's width and height, and center each within its own quadrant with large genuinely transparent padding; absolutely no artwork may cross the horizontal and vertical midlines. Preserve the four distinct poses: upright sitting, leaning, reclining, asleep. Preserve the small chest mole only in the two upper seated frames, and do not add it in the lower two reclining/sleeping frames. Preserve Kinaiya's face, hair, white streaks, fully opaque skin and clothing, robe, limbs, and exact indigo and white palette. Keep the 1536 by 1024 layout. No backgrounds, grid, text, or extra figures.
