# Oběhy realtime worker in Rust

The port of `src/obehy/realtime/` to a standalone binary (`docs/RUST_PORT.md`). Python stays the
reference until the switch.

| Crate | What | May depend on |
|---|---|---|
| `obehy-core` | time, geo, model + facts JSON, config (policy, manifests), `py` (Python semantics); later index, infer, timeline, core, publish, vehicles | no I/O crate |
| `obehy-sources` | connectors: bytes in, facts out (R2) | `obehy-core`; no async, no I/O |
| `obehy-rt` | the `obehy-rt` binary: shell, store, archive, commands | everything |

One file per Python module; stubs name the module they will port.

## Build and check

Stable MSVC toolchain (`rust-toolchain.toml`), linking with the installed VS2026 MSVC. In `rust/`:

```
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
```

The dependency direction is a test: `obehy-rt/tests/dependency_direction.rs` walks Cargo.lock
and fails if `obehy-core` reaches an I/O crate (tokio, postgres, reqwest, hyper, ...) or
`obehy-sources` reaches one or an async runtime.

## Python parity fixtures

`obehy-core/tests/fixtures/` is written by the Python side itself:

```
uv run python rust/tools/gen_fixtures.py
```

It covers `float.__repr__` and `json` float text, `round`/`hypot`/float `%` and `//`, the
Prague zone (tzdata release, TZif bytes, every transition 1970-2100, schedule times and local
readings), `fromisoformat`, `timedelta` rounding, the source-time readers, haversine, the loaded
policy and manifests, and facts JSON (hand-made plus real payloads decoded from the archives
under `data/rt-raw-prod`, `../rt-data/rt-raw` and `data/rt-raw`). Regenerate it when the
Python side changes, and on a tzdata bump after copying the new TZif (see
`obehy-core/src/time/zone.rs`).

## Reading policy, manifests and facts

```rust
use obehy_core::config::{Policy, manifest};
let policy = Policy::load(&data_dir.join(obehy_core::config::policy::POLICY_FILE))?;
let skew = policy.time.max_clock_skew();
let rail_delay = policy.time.max_delay_s.get("rail");        // ByMode values are f64
let sources = manifest::load_sources(&sources_dir, &manifest::RECORDER_FILTERS)?;

use obehy_core::model::json::{facts_from_json_text, facts_json_text};
let facts = facts_from_json_text(stored)?;                    // rt.observation.facts
assert_eq!(facts_json_text(&facts), stored);                  // byte-identical to Python
```

Python semantics the port must keep (half-even `timedelta` arithmetic, `round`, `min`/`max`
ties, floored `%`, CPython `hypot`, float repr) are in `obehy_core::py` and
`obehy_core::time::TimeDelta`; `docs/PORTING_HAZARDS.md` lists where each is needed.
