//! Embed a digest of this crate's own source, and of the Python modules it
//! mirrors, in the binary.
//!
//! ``kiro_crew.mcp_gateway.stub_impl`` computes the same digest over the
//! checkout and launches the binary only when the two match, so a build left
//! over from before a ``git pull`` is never mistaken for the current stub.
//! Keep the algorithm in step with ``stub_impl._source_digest``.

use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};

fn rust_sources(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for e in entries.flatten() {
        let p = e.path();
        if p.is_dir() {
            rust_sources(&p, out);
        } else if p.extension().is_some_and(|x| x == "rs") {
            out.push(p);
        }
    }
}

/// The Python modules this crate mirrors, as crate-relative POSIX paths. A
/// change to any of them means the port may have diverged, so it changes the
/// digest and a binary built before it keeps the Python stub. Keep in step
/// with ``stub_impl._MIRRORED_PYTHON``.
const MIRRORED_PYTHON: &[&str] = &[
    "../../src/kiro_crew/code_fingerprint.py",
    "../../src/kiro_crew/json_line.py",
    "../../src/kiro_crew/jsonl_util.py",
    "../../src/kiro_crew/mcp_caller.py",
    "../../src/kiro_crew/mcp_cleanup.py",
    "../../src/kiro_crew/mcp_gateway/claim.py",
    "../../src/kiro_crew/mcp_gateway/hashing.py",
    "../../src/kiro_crew/mcp_gateway/read_limits.py",
    "../../src/kiro_crew/mcp_gateway/secret_uri.py",
    "../../src/kiro_crew/mcp_gateway/shutdown_budget.py",
    "../../src/kiro_crew/mcp_gateway/stub.py",
    "../../src/kiro_crew/mcp_gateway/transport.py",
    "../../src/kiro_crew/session_pid_sig.py",
    "../../src/kiro_crew/session_token_sig.py",
];

fn main() {
    let root = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").expect("manifest dir"));
    let mut files = vec![
        root.join("Cargo.toml"),
        root.join("Cargo.lock"),
        root.join("build.rs"),
    ];
    rust_sources(&root.join("src"), &mut files);
    let mut rel: Vec<(String, PathBuf)> = files
        .into_iter()
        .map(|p| {
            let r = p
                .strip_prefix(&root)
                .expect("under the crate")
                .components()
                .map(|c| c.as_os_str().to_string_lossy().into_owned())
                .collect::<Vec<_>>()
                .join("/");
            (r, p)
        })
        .collect();
    for m in MIRRORED_PYTHON {
        rel.push(((*m).to_string(), root.join(m)));
        println!("cargo:rerun-if-changed={m}");
    }
    rel.sort();
    let mut h = Sha256::new();
    for (r, p) in &rel {
        h.update(r.as_bytes());
        h.update([0u8]);
        h.update(std::fs::read(p).expect("readable source"));
        h.update([0u8]);
    }
    let hex: String = h.finalize().iter().map(|b| format!("{b:02x}")).collect();
    println!("cargo:rustc-env=KIROCREW_STUB_RS_SOURCE_DIGEST={hex}");
    println!("cargo:rerun-if-changed=Cargo.toml");
    println!("cargo:rerun-if-changed=Cargo.lock");
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed=src");
}
