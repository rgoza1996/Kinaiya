# Bounded Meta API qualification

Part of [#196](https://github.com/rgoza1996/Kinaiya/issues/196). This is a local
development admission gateway, not production model management or a billing
statement. It never stores credentials or request/media contents in its ledger.

## Retained owner ceiling

Start the gateway with the existing ledger and an explicit lower ceiling:

```sh
python scripts/muse_budget_proxy.py --ledger /absolute/existing/meta-budget.json --limit-usd 10
```

The ceiling becomes the minimum of the ledger's existing ceiling and the
argument. Invalid, nonfinite, zero or greater-than-$25 values refuse. It cannot
raise a ceiling, reset earlier usage, clear an uncertain reservation or replace
a billing floor. A file lock permits only one gateway owner. Quiesce active
requests before restarting a shared gateway; an old ledger state labelled
in-flight is not proof that a process still owns a live request.

Admission includes text's validated cache-aware estimate, conservatively metered
buffered audio, optional media estimates, every unsettled reservation, and the
next reservation. The higher of settled estimates and reported billing is the
floor. The ordinary-Spark comparison and legacy audit counters are not invoices
and must not be presented as actual spend. The retained combined ceiling applies
to every category. This gateway does not account for another provider's ledger;
reconcile any earlier OpenRouter spend before authorizing additional paid tests
there. The October 31 qualification did not make OpenRouter requests.

`GET /budget/summary` returns a bounded numeric snapshot and estimate label,
including the accounted total with reservations. It omits all historical call
records. Use this for qualification and display; a large private audit ledger
is unnecessary for deciding whether a new test fits its retained ceiling.

## Optional single-image tests

`--enable-media-smoke` enables two additional bounded routes. It is off by
default and is not an Agent tool grant:

| Route | Exact accepted operation | Reservation |
| --- | --- | --- |
| `/v1/images/generations` | `muse-image-1.0`, one image, bounded text prompt, `b64_json`; no extra parameters. | $0.02 |
| `/v1/responses` | `sam-3.1`, one inline PNG up to 1024×1024 and 2 MiB, one short concept, buffered stateless output. | $0.005 |

Media has a $0.10 sublimit inside the owner's combined ceiling. Reservations use
twice the [published image prices](https://dev.meta.ai/docs/pricing-rate-limits):
$0.01 per Muse image and $0.0025 per SAM image. Muse Image's built-in grounding
is included in its per-image price. SAM video, file IDs, remote image URLs,
uploads, arbitrary models, multiple generations and streaming are refused by
this smoke surface. There is no automatic retry.

Only complete bounded results settle a reservation; HTTP/transport errors,
malformed/unknown results, incomplete bodies and lost responses retain it.
Settled media remains conservatively charged at the full reservation amount.
No Contributor header is forwarded to Standard voice or media calls. The only
upstream is pinned HTTPS with redirects disabled. Read the returned artifact
and use the official SAM parser before claiming that a decoded mask is valid;
an HTTP 200 alone establishes neither result quality nor production readiness.

`tests/unit/test_muse_budget.py` checks the lower combined ceiling, preservation
of previous usage/uncertainty, malformed requests, sublimits, pinned routes,
headers and retained reservations without calling Meta. Other tests continue
to cover text counts, usage and voice duration bounds. Real-model observations
are separate opt-in evidence, using public synthetic media only.
