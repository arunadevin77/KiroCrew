# kirocrew-stub-rs

A Rust port of `kiro_crew.mcp_gateway.stub`, the shim kiro-cli runs for each stubbed MCP server. When its binary is built, the gateway uses it by default; when it is not, the Python stub runs.

## Build

```bash
cd packages/kirocrew-stub-rs && cargo build --release --locked
```

The gateway looks for `packages/kirocrew-stub-rs/target/release/kirocrew-mcp-stub-rs` inside the checkout.

## Choose

| `mcp_gateway.stub_impl` in `config.json` | Stub |
|---|---|
| `auto` (default) | Rust if the binary is executable, else Python, no warning |
| `rust` | Rust; a missing binary keeps Python and logs why |
| `python` | Python always |

Linux only; macOS and Windows always run the Python stub. The binary embeds a digest of this crate's source and of the Python modules it mirrors (`stub.py` and the product modules it imports for wire, identity and pooling behaviour; the list is `MIRRORED_PYTHON` in `build.rs`), and the gateway launches it only when that matches the checkout, so a build from before a `git pull` keeps the Python stub and logs that it needs a rebuild.

A stub without a verified session token hands itself to the Python stub. If it cannot (no recorded interpreter, or the exec fails), it registers with no session key rather than trusting `KIROCREW_SESSION_KEY`.

CI builds the crate and runs `cargo test` plus the conformance suite in the `Rust MCP Stub Gate (kirocrew-stub-rs)` job; every other job runs the Python stub.

## Tests

```bash
cargo test
.venv/bin/python -m pytest test/test_stub_rs_conformance.py
KIROCREW_TEST_STUB_BIN=$PWD/packages/kirocrew-stub-rs/target/release/kirocrew-mcp-stub-rs \
  .venv/bin/python -m pytest test/test_stub_broker_reconnect.py::test_session_survives_a_broker_restart
```

## Measured (Linux, isolated pod, 3 sessions x kirocrew-core + kirocrew-guide)

| | Python | Rust |
|---|---|---|
| RSS per stub | 29.3–29.9 MB | 3.0–3.6 MB |
| Stub RSS per session (2 stubs) | 59.3–59.4 MB | 6.6–6.7 MB |
| Start to `initialize` reply, p50 | 122–127 ms | 21–23 ms |
| `tools/list` p95, 6000 calls pooled | 1.37 ms | 1.23 ms |
| `learn_list` / `find_setting` p95 | 103.4–106.2 ms | 103.0–105.1 ms |

## Handed to the Python stub

The Rust stub execs into the Python stub (same flags, env and stdio, through the interpreter the rewriter recorded) instead of porting these:

- A stub without a verified session token. Identity rung 4 (`session_pid_<pid>.txt` walk) and the shared-host-pid veto on rung 3 stay in Python. Every Kiro Crew session stamps a token, so this is rare.
- The direct-exec fallback while the gateway cannot serve: refusing an unresolvable `secret://` env (exit 78) and keeping the session token for Kiro Crew's own control plane after gatewayd's vetting. The Python process runs only `fallback_exec`, then becomes the backend.

An overlay that recorded no interpreter keeps the Rust behaviour: the fallback still refuses `secret://`, but always drops the token.

## Not ported

- Windows (named pipe transport, child-process fallback).
- `KIROCREW_MCP_RECONNECTS` metric and the fallback-rate reader (`fallback_counts`, which gatewayd still runs from Python).
