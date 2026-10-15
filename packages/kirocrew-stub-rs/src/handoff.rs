//! Hand this stub over to the Python implementation for what the port does
//! not carry.
//!
//! Two paths stay Python-only, and the Rust stub execs into the Python stub
//! (same flags, same env, same stdio) instead of approximating them:
//!
//! * Identity without a verified session token. The Python ladder's
//!   co-tenancy veto on ``KIROCREW_SESSION_KEY`` (``host_pid_is_shared``) and
//!   its ``session_pid_<pid>.txt`` walk read hardened, signed mappings; a
//!   wrong session name is worse than none, so no shortcut is taken here.
//!   Every Kiro Crew session stamps a token, so this path is rare.
//! * The direct-exec fallback while the gateway cannot serve. The Python
//!   ``fallback_exec`` refuses a ``secret://`` env it cannot resolve and keeps
//!   the session token for Kiro Crew's own control plane only after
//!   gatewayd's vetting. The handed-over process runs ``fallback_exec`` alone
//!   (no second handshake) and then execs the backend, so it costs no memory
//!   once the backend runs.
//!
//! Both need the interpreter the rewriter recorded in the install context. An
//! overlay without it, or an exec that fails, keeps the Rust behaviour, and
//! that behaviour is fail-closed: the stub registers with no session key
//! (``payload::resolve_session_key`` never reads ``KIROCREW_SESSION_KEY``).

use crate::args::{Args, STUB_FLAGS_FLAG, encode_b64_json_list};
use crate::log;
use crate::payload::InstallContext;
use crate::util::{ERROR, INFO};
use std::os::unix::process::CommandExt;

const STUB_MODULE: &str = "kiro_crew.mcp_gateway.stub";

/// Runs only the Python stub's fallback: parse the same flags, then exec the
/// backend exactly as ``python -m kiro_crew.mcp_gateway.stub`` would.
const FALLBACK_PROGRAM: &str = "import sys\n\
from kiro_crew.mcp_gateway import stub\n\
stub.fallback_exec(stub._parse_args(sys.argv[1:]))\n\
raise SystemExit(1)\n";

fn launcher(a: &Args) -> Option<Vec<String>> {
    let argv = InstallContext::from_args(a)?.python_argv;
    if argv.first().is_none_or(|exe| exe.is_empty()) {
        return None;
    }
    Some(argv)
}

fn exec(mut argv: Vec<String>, a: &Args, what: &str) {
    argv.push(format!(
        "{STUB_FLAGS_FLAG}={}",
        encode_b64_json_list(&a.python_flags)
    ));
    let exe = argv.remove(0);
    let err = std::process::Command::new(&exe).args(&argv).exec();
    log!(
        ERROR,
        "could not hand {what} to the Python stub ({exe:?}: {err}); the Rust stub carries on without a session key"
    );
}

/// Replace this process with the Python stub. Returns only if that failed or
/// the overlay recorded no interpreter.
pub fn run_python_stub(a: &Args, why: &str) {
    let Some(mut argv) = launcher(a) else {
        return;
    };
    log!(
        INFO,
        "{why}; handing this stub to the Python implementation"
    );
    argv.extend(["-m".to_string(), STUB_MODULE.to_string()]);
    exec(argv, a, "the stub");
}

/// Replace this process with the Python stub's ``fallback_exec``. Returns only
/// if that failed or the overlay recorded no interpreter.
pub fn run_python_fallback(a: &Args) {
    let Some(mut argv) = launcher(a) else {
        return;
    };
    argv.extend(["-c".to_string(), FALLBACK_PROGRAM.to_string()]);
    exec(argv, a, "the fallback");
}
