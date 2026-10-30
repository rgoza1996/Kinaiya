# Inter font assets

These three unmodified static TrueType fonts come from the official
[Inter 4.1 release](https://github.com/rsms/inter/releases/tag/v4.1), published
by the Inter project on 2024-11-16. Copyright (c) 2016 The Inter Project Authors.
They are distributed under the SIL Open Font License 1.1; [OFL.txt](OFL.txt)
is the release archive's complete `LICENSE.txt`, copied byte-for-byte.

## Provenance

- Repository: <https://github.com/rsms/inter>
- Release tag: `v4.1`, resolved on 2026-10-29 to commit
  `e3a3d4c57d5ecc01453a575621882a384c1995a3` using the official GitHub API.
- Release ID: `185731254`; asset ID: `206862670`.
- Download: <https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip>
- Archive size: `33707794` bytes, matching the release API metadata.
- Archive SHA256:
  `9883fdd4a49d4fb66bd8177ba6625ef9a64aa45899767dde3d36aa425756b11e`.
- Selected members: `extras/ttf/Inter-Regular.ttf`,
  `extras/ttf/Inter-SemiBold.ttf`, `extras/ttf/Inter-Bold.ttf` and `LICENSE.txt`.

The archive was downloaded over HTTPS from the official release. The API did
not publish an asset digest, and the release is not marked immutable. These
locally computed hashes pin the actual bytes obtained; they are not publisher
signatures or an independent reproducible-build attestation. The tag identifies
the release source, not proof that these binaries were rebuilt from that tree.

## Bundled files

| File | Weight | Bytes | SHA256 |
| --- | --- | ---: | --- |
| Inter-Regular.ttf | 400 | 411640 | `40d692fce188e4471e2b3cba937be967878f631ad3ebbbdcd587687c7ebe0c82` |
| Inter-SemiBold.ttf | 600 | 419744 | `78a843fade9d4612a5567302fb595b56976eb5fcebf4fea5a5912d638bafcde3` |
| Inter-Bold.ttf | 700 | 420428 | `288316099b1e0a47a4716d159098005eef7c0066921f34e3200393dbdb01947f` |
| OFL.txt | — | 4380 | `262481e844521b326f5ecd053e59b98c8b2da78c8ee1bdbb6e8174305e54935a` |

Static SFNT inspection verified the TrueType header, bounded table locations,
`OS/2.usWeightClass` values 400/600/700, and absence of a variable `fvar` table.
Their PostScript names are `Inter-Regular`, `Inter-SemiBold`, and `Inter-Bold`.
All three report internal version `Version 4.001;git-9221beed3`; this is retained
as supplied in the archive labeled 4.1, without relabeling or modification.

Flutter registration uses family `Inter` and the explicit weights above; the
application's `pubspec.yaml` owns that registration. These assets need no font
installation, runtime download, variable-axis configuration, or font conversion.
