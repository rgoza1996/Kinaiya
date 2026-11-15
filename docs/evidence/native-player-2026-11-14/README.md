# Public native video presentation qualification

This supplies the browser-control evidence requested by
[#476](https://github.com/rgoza1996/Kinaiya/issues/476). The existing repair
`049f5f8f57fc0208b128bd3fbef87f1804fad34a` is already on main. No presentation,
authentication, media-loader or playback code was changed by this qualification.

The frontend is the actual `kinaiya-flutter-web` build from
[CI run 43660840023](https://github.com/rgoza1996/Kinaiya/actions/runs/43660840023),
source `cb06ce9e2f9c69146cf8cc37d2254ae04e1dd412`, artifact `14139962594`.
Its complete 45,343,006-byte ZIP was verified against SHA-256
`59f878655152880ad649edba35a88bf49ecb8b97b0f27a0d400cb30a223d6435`.
`git diff` confirms its entire Flutter source is unchanged from main
`4e34f94df9845717eb5896aec61d4ea6f9d24c19`.

The native `asset_handler` served that unmodified build on an owned loopback
port. Separate real Session/Agent services, SQLite artifact/native-media stores
and authenticated HTTP routes served an eight-second public test-pattern MP4.
A held scripted Agent and one scripted Converse reply exercise the ordinary
named Converse-session path. No real model, microphone, private media, SAM,
translation, task completion or owner-service change is claimed.

## Actual browser observations

| Browser viewport | Native player | Observed controls |
| --- | --- | --- |
| 1920×1080 | 426.65625×240 | Inside the result pane; play, pause and seek worked |
| 800×900 | 426.65625×240 | Player and controls remained inside the responsive pane |
| 390×844 | 290×163.125 | Labels wrapped; play, pause and seek remained usable inside the pane |

Before explicit **Original video** loading, no video element existed. The
actual loaded native element reported `controls=true`, `muted=true`,
`autoplay=false`, `paused=true`, `currentTime=0`, duration 8 seconds and decoded
640×360 media. Its source was a browser-owned blob, not a model-supplied URL.
The desktop play/pause observations advanced to 0.1481/0.1606 seconds, then a
native scrubber click sought to 3.99314 seconds. At phone width, play/pause
advanced to 4.1568/4.1768, then seeking moved back to 1.723577 seconds while
remaining paused and muted. These are actual UI actions and read-only DOM
measurements, not JavaScript calls to `play()`, `pause()` or `currentTime` setters.

Screenshots were visually inspected during the browser check; screenshot files
were not exported. [observations.json](observations.json) preserves the measured
values, build/native identities and setup limits. The adjacent
[public test pattern](public-test-pattern.mp4) is the exact 799,858-byte input,
SHA-256 `9eca39fa8485bce863aa4c654d06e7f4c8caf132dbc048ed05c25bee49fb13b2`.
Both native media variants deliberately contain this same public fixture; no
SAM processing is represented by the inherited product button label.

Closing the result removed the native video element (count 0). The temporary
viewport override was reset, agent-created browser tabs closed, both fixture
HTTP listeners stopped, and the original scripted work and stores closed with
positive cleanup. Earlier setup attempts remain separately recorded: the first
used an Agent channel and its cleanup observer was canceled; the second had not
acquired the normal named Converse session. Neither is counted as a successful
player test.

## Remaining owner acceptance

This was a verified build served through the native static handler in an
isolated development deployment. The owner's persistent frontend promotion
path was **not** changed. Keep that deployment acceptance separate before
closing #476. The browser evidence does not establish private-video playback,
semantic translation, native Android delivery, audio quality or Agent success.
