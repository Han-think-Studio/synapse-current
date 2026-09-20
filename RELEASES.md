# Synapse mini releases

This file records the public-release cleanup and the small, portable edition of Synapse.

## Public repository cleanup — 2026-09-20

- The public repository was rebuilt from a clean Synapse mini release tree.
- The first clean public root commit is `a310a1d`.
- Previous public and backup branches and tags were removed before promotion.
- GitHub Releases and tags were audited before publication; no previous releases or tags existed to archive or delete.
- The internal Synapse OS working tree, `spec/`, handoff records, private logs, and private test artifacts are not part of this repository.

## v0.1.0-mini (pre-release)

The first public mini edition provides:

- a deterministic, read-only plan/check/demo workflow;
- ten recorded examples and multilingual HOWTO/RESULTS material;
- portable Windows launchers that detect their own checkout root;
- English, Korean, and Chinese README files; and
- a custom Personal Non-Commercial License.

The release is intended as a compact, inspectable entry point. It does not represent approval or publication of the full internal Synapse OS project.

## Use boundary

Personal and non-commercial use is permitted under `LICENSE`. Commercial use, paid services, hosted offerings, and commercial redistribution require written permission.
