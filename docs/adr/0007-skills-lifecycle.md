# ADR 0007: Skills lifecycle (bounded SKILL.md filesystem)

Accepted for the M1A ecosystem slice, against Roadmap.md Section 31
(skills ecosystem) and the architectural clarification on autonomous
capability expansion. The roadmap is unchanged.

## Implemented

Local skill lifecycle in `packages/ecosystem/skills.py`, verified by
`tests/integration/test_ecosystem_skills.py`:

- Install from a local directory (`install`) or a local `.zip`/`.tar.gz`
  archive (`install_archive`), plus `update`/`update_archive`, `enable`,
  `disable`, `uninstall` (`remove` is a compatible alias), `rollback`,
  `inspect`, `list`, `versions`, `verify`, `load`, and `load_resource`.
- Typed, pinned source references (`SourceRef`): `local`,
  `github:<owner>` (full 40-hex commit only), `clawhub:<scope>`
  (explicit version only). Dicts are rejected; credential-bearing URLs,
  unsupported schemes, query/fragment-bearing URLs, and moving refs are
  rejected. Non-local installs require a pinned ref whose name matches the
  `SKILL.md` name. Provenance records kind/namespace/name/subdir/ref/raw;
  no secrets are ever carried.
- `SKILL.md` frontmatter parsing with hard bounds, no YAML aliases, no
  duplicate keys, string-only keys, and validated `name`/`description`/
  `version`. Directory, zip, and tar inputs accept absolute or relative
  roots; every member name is validated (no absolute paths, no `..`, no
  symlinks/hardlinks/devices/FIFOs/encrypted entries), with per-file, total,
  and count caps enforced before any write. Extraction is manual and
  containment-checked; `extractall` is never used.
- Content-addressed immutable snapshots (`.versions/`, keyed by install
  digest) written before every update; `rollback` revalidates snapshot
  bytes against the snapshot digest and restores actual pinned files
  (staging swap), never just metadata. `verify` recomputes the digest and
  also rejects added symlinks/special files.
- All deletes are confined to manager-owned `installed/` state; registry,
  versions store, and anything outside the root are never deleted.
- Lifecycle mutations require explicit `authority="owner"`. Discovery
  (`list`/`inspect`/`versions`) and invocation loads take no authority,
  and content never confers authority. Command exposure is metadata only
  (`bare_invocable`; reserved names stay qualified-only); skills are
  non-authorizing and never executed (no imports, subprocesses, or
  dependency installs).
- Truthful setup gating: `ready` and `needs_adapter` enable; `needs_setup`
  (missing executables, undeclared pip prerequisites) and `unsupported`
  (foreign platform markers) install inactive and refuse `enable`, with
  reassessment on every `enable`. Disabled skills refuse `load`.
- Deterministic namespace collisions: identical display names coexist as
  distinct `<namespace>/<name>` ids; reinstalling an existing id fails
  with the installed version/digest named; snapshots are inspectable via
  `versions()`.
- Opt-in pinned GitHub fetch (`fetch_github_pinned`, public
  `codeload.github.com` only, no redirects, bounded download, same
  all-member validation). An explicit `http(s)://127.0.0.1` base exists
  solely as an offline test seam.

## Explicitly unsupported

ClawHub live API resolution, private GitHub sources with embedded
credentials, unpinned remote fetches, marketplace install modes, and any
execution of skill code or dependencies. These paths raise `SkillError`
naming the limitation; pass an already-fetched local directory instead.
Remote skill content is untrusted advice even when the transport is pinned:
a hash detects change, never trustworthiness.

## Verification

`tests/integration/test_ecosystem_skills.py` (36 tests): absolute-root
install, namespace/version/frontmatter traversal, dict refs, YAML
alias/duplicate rejection, symlink/FIFO rejection, zip/tar traversal/
symlink/oversize/count rejection, tamper detection (edited bytes, added
symlinks, update refusal), owner-vs-Agent authority, namespace collisions,
duplicate-install inspectability, two-step update with byte-for-byte
rollback to each prior version, rollback without history, setup/unsupported
enable gating, disable/re-enable, reserved names, uninstall containment,
resource containment, loopback redirect refusal, loopback tarball fetch
(installing the fetched tree), and explicit remote-mode failures. Full
`tests/integration/` suite and `ruff check` pass.
