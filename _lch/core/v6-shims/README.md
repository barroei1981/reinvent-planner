# v6 Deprecation Shims

Skills in this folder are forwarders kept for backward compatibility with v6 skill IDs.
Each one holds no logic of its own — it forwards to the skill that replaced it, pinning
the legacy output contract so existing callers keep working.

| Shim                              | Forwards to                              |
| --------------------------------- | ---------------------------------------- |
| `lch-editorial-review`           | `lch-review` (structure + prose lenses) |
| `lch-editorial-review-prose`     | `lch-review` (prose lens)               |
| `lch-editorial-review-structure` | `lch-review` (structure lens)           |
| `lch-review-adversarial-general` | `lch-review` (adversarial lens)         |
| `lch-review-edge-case-hunter`    | `lch-review` (edge-case lens)           |
| `lch-review-verification-gap`    | `lch-review` (verification-gap lens)    |

`lch-editorial-review` keeps its `customize.toml` so existing team and user
overrides still resolve; the shim forwards those resolved values to `lch-review`.

External module repos (gds, loop, tea, bmb, os-utils) still invoke these IDs, so they
ship by default. Removal rides the v7 cut — never a 6.x minor.

The folder is grouping only: the installer discovers skills recursively and installs each
one under its own `name`, so nesting here does not change any installed path or skill ID.
A future install option will let users include or exclude this folder before it is removed
outright.
