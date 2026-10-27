# ADR 0007: Bounded local skills lifecycle

Implemented foundation for Roadmap.md sections 31.2–31.4 and issue #21.
This module is not the full M1A delivery or autonomous capability acquisition.
It is not yet exposed through Agent tools, API, commands, or Flutter controls.

## Behavior and authority

`packages/ecosystem/skills.py` provides install from a local directory or
zip/tar archive, inspect/list, enable/disable, update, remove, verify, preserved
versions and rollback. Instructions load on demand, followed by individual
supporting resources. Files, scripts, assets and licenses are preserved;
installation and inspection never execute them or install dependencies.

Lifecycle changes require the explicit owner authority supplied by a trusted
caller. The string argument is an internal interface, not authentication for
a public endpoint. Skill text and `allowed-tools` metadata confer no authority.
Installs start disabled. Unknown prerequisites never become `ready`.
`needs_setup`, `needs_adapter` and `unsupported` refuse activation. No foreign
runtime adapter is implemented. Model/user invocation controls apply to both
instructions and resources; manual-only content refuses model invocation.
Built-in command names remain reserved; command exposure is only metadata here.

Typed source references distinguish local, GitHub and ClawHub provenance.
GitHub requires a full 40-hex commit; ClawHub references require an explicit
version but do not resolve through a live registry. GitHub repository names
and selected skill names are distinct, so a pinned monorepo subdirectory may
contain a differently named skill. Credential-bearing URLs, moving refs,
unsupported hosts/schemes, queries and fragments are rejected. Provenance uses
validated canonical fields rather than copying arbitrary input URLs.
The opt-in GitHub fetcher permits public codeload HTTPS without redirects;
an explicit loopback base exists for deterministic tests. It validates every
archive member, including those outside the selected skill directory.

## Filesystem and input boundaries

The supported implementation requires POSIX directory descriptors and
`O_NOFOLLOW` (verified on Linux). Source trees and resources are captured
through pinned descriptors, including every ancestor. Metadata, digests and
installed files derive from the same captured bytes. Loading and rollback
likewise return/copy the verified snapshot rather than rereading mutable
source paths after validation. Symlinks and special files are rejected.

Manager control directories are created without following ancestor links.
Registry, installed store and versions store reject existing symlinks, and
control state must be disjoint from the granted Agent workspace. Registry
reads are bounded; writes use exclusive temporary files, fsync and replace.
The private control directory must remain inaccessible to untrusted Agent
code. These checks do not defend against an administrator replacing private
control directories during lifecycle operations.

Directory entries, file bytes, total bytes and archive members have explicit
caps. Zip entries use bounded reads. Tar uses a decompressed stream cap before
parsing PAX/long-name metadata, preventing compressed headers from bypassing
the file limit. Extraction validates names/duplicates/types before writing
package files and never calls `extractall`. YAML rejects aliases, duplicate
keys and excessive nesting/node counts. Frontmatter delimiters occupy a whole
line, allowing quoted descriptions containing `---`.

Updates preserve the old content-addressed bytes and metadata. Rollback checks
the snapshot digest and copies those captured bytes. This is a single-owner
module: cross-process mutation locking, transactional recovery across registry
and directory swaps, task version pins, local-fork handling and garbage
collection remain required before exposing concurrent lifecycle operations.

## Verification and limits

The focused suite comprises `test_ecosystem_skills.py` and
`test_skill_boundaries.py`: 53 tests. It exercises local/archive installation,
loopback pinned fetch and monorepo selection, actual file-swap symlink races,
control/ancestor symlinks, source and rollback snapshot consistency, invocation
restrictions, tampered invocation metadata, parser/decompression caps,
namespace collisions, setup/adapter refusal and byte-for-byte rollback.
Only synthetic content is used. A newly added parser test initially expected
no terminal newline; its expectation was corrected to the existing body
normalization. No production behavior or pre-existing check was weakened.

Live ClawHub resolution, authorized private repository transport, genuine
OpenClaw/Hermes conformance fixtures, dependency installation/execution,
runtime activation scopes, concurrent lifecycle controls and task pins remain
unimplemented. A valid portable package does not establish upstream runtime
compatibility or prove an executable capability works. Tracker #3 stays open.
