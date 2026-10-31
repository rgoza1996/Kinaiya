# ADR 0071: Constant diagnostics at UI decoding boundaries

Status: accepted for the bounded #169 diagnostic slice.

Several distinct invalid `ui_spec_json` values produce the same existing
failure message. Publication history and receipts alone cannot distinguish
syntax, size, Unicode, duplicate-key and shape failures, and do not establish
the cause of repeated view creation.

Tag existing decoder rejections with constant reasons, preserve validation and
guard order, and carry an allowlisted stage/reason pair into the authenticated
failed outcome. Retain current error text and `ValueError` compatibility. Do
not serialize model text, parser documents, keys, positions or exception causes.
Inspection metadata must not block failure reporting or become execution proof.

The task still stops on provider decoding failure without retry or automatic
finish. Earlier valid artifacts remain usable without implying task success.
No general reliability or historical-cause claim follows. See the
[contract and verification mapping](../ui-decode-diagnostics.md).
