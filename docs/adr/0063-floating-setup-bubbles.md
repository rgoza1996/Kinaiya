# 0063: Floating Converse setup controls

Issues [#148](https://github.com/rgoza1996/Kinaiya/issues/148) and
[#152](https://github.com/rgoza1996/Kinaiya/issues/152) request randomized starting
locations, slow physical drift, independent hover holds, dragging/flinging, and
expanding options modals. Follow-up
[#154](https://github.com/rgoza1996/Kinaiya/issues/154) moves Close into the field
and adds empty-space dismissal. Eight bubbles replace the fixed controls:
Setup, Model, Reasoning, Inputs, Speech, Decisions, View, Close. Their host reads
and separately saved local visibility preference retain ADR0060's boundaries.
This change owns presentation only.

A bounded local simulation gives circles slow velocities and wall, obstacle and
pair collisions. Newly mounted setup gets fresh random starts. Only the central character area remains reserved in wide layouts. Narrow layouts have a taller scrollable field; large text increases
bubble diameter. Impossible geometry falls back to readable flow. Resize creates
a new valid arrangement while preserving control identity.

Hover, press and keyboard focus hold only the affected body; a held circle acts
as an immovable collision surface. Pointer dragging follows a swept collision
path. Release caps speed at 600 logical pixels/second and damps it toward ordinary
slow drift. A released body ignores automatic re-entry beneath the stationary
release pointer until it has left that pointer once, so hover cannot swallow its
launch. Cancel, responsive replacement and backgrounding discard gesture
ownership and stale launch velocity. Pointer movement releases old keyboard focus
when it is outside that focused bubble. Keyboard order follows declaration order,
independently of randomized position.

Clicking a bubble holds and hides its source body while an overlay grows from its
actual bounds into the options modal over 260 ms. Other bodies continue drifting
behind the input barrier. The panel retains its final layout inside the animated
clip; intermediate bounds cannot squeeze its controls. Close, outside click and
Escape reverse the animation and restore the invoking bubble's focus. Resize
rebases the return bounds. Close is a normal floating, draggable circle that
dismisses setup without saving. A genuine tap on empty space also dismisses
setup; transparent circle corners count as empty. Child bubble taps, pans and
scrolling win their gesture arenas over the background tap detector. The first
outside tap on an expanded panel shrinks only that panel, and the next empty tap
can leave setup. Existing voice Stop controls stay outside the dismissible field.
Pending local saves block Close, background, route-barrier and Escape dismissal
until their existing success or uncertain-save outcome is reconciled. Opening and closing options never saves the local
view preference; View retains explicit Save/Cancel and uncertain-save feedback.

Animations off, reduced motion and OS disable-animations suppress autonomous
movement and morph transitions; direct dragging remains available without fling.
Inactive lifecycle and TickerMode suppress physics frames. Background elapsed time
is discarded and simulation step time is bounded. Dismissal disposes tickers.

Verification maps #152/#154 to `bubble_drag_physics_test.dart` (held collisions,
sweeps, velocity and recovery), `bubble_drag_field_test.dart` (gestures, ownership,
visibility and motion gates), `floating_bubbles_test.dart` (actual setup holds,
continued neighbor motion, focus, availability, resize and accessibility), and
`model_bubble_morph_test.dart` (source/intermediate/target/reverse bounds, motion
gates and resize). `setup_dismiss_test.dart` covers empty/corner taps, mouse/touch Close dragging,
keyboard activation, nested panel dismissal, narrow scrolling, voice-control hit
testing and pending-save outcomes. Existing setup/navigation tests retain GET-only, session,
voice-owner and preference persistence checks. All fixtures are synthetic.
Continuous motion tests advance bounded frames instead of waiting to settle.

## Deterministic release regression (#163)

[#163](https://github.com/rgoza1996/Kinaiya/issues/163) preserves two intermittent
post-merge failures: [run 41157044621](https://github.com/rgoza1996/Kinaiya/actions/runs/41157044621)
and [run 41163861835](https://github.com/rgoza1996/Kinaiya/actions/runs/41163861835).
The old setup test required more than 30 pixels of net displacement after 120
frames in a fresh random layout. It observed 27.0164 and 22.4812 pixels. Net
displacement after collision/damping does not establish whether a release was
swallowed. The exact failed layouts were not retained, so their individual
collision histories remain unknown.

A pinned Flutter 3.47.8 `VelocityTracker` diagnostic measured the old mouse
samples (0, 110, 111, 112 pixels at 16 ms intervals) as about **-3003.125 px/s**,
opposite the test's forward clear-path check. The replacement uses a recognition
jump followed by ten steady 8-pixel samples at 16 ms, measured as **500 px/s**.
Optional injected entropy supplies eight fixed positions and initial velocities
through the real setup widget; production still creates fresh `Random()`.
No collision, hover, damping or gesture code changed.

The corrected widget test observes the released circle pass beneath the stationary
pointer, leave it and remain unheld on every intervening frame. It checks neighbor
motion, no accidental modal and normal hover after deliberate pointer movement.
Replacing the stationary-release hover guard with unconditional hover capture
makes this test fail with `Stationary pointer must never relatch Model.` The
source was restored and the full Flutter suite passed. This tests the actual
regression instead of relaxing the old distance threshold.

This presentation change supplies no example timeline or generated rationale.
The separate [Decisions traceability requirements](../decisions-traceability.md)
subsequently gained the native capture/API and owner inspection implementation
in [PR160](https://github.com/rgoza1996/Kinaiya/pull/160), documented by
[ADR0066](0066-decisions-owner-window.md). See the [status ledger](../implementation-status.md#decisions-inspection)
for its actual scope and remaining qualification.
