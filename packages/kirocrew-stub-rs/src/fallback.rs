//! Degrade path: the fallback audit record and the in-place exec of the real
//! MCP backend (``log_fallback`` / ``fallback_exec``).

use crate::args::{Args, crew_home};
use crate::log;
use crate::payload::{STUB_SESSION_TOKEN_ENV, parse_env_file};
use crate::util::{ERROR, WARNING, iso_utc, now_f64};
use serde_json::json;
use std::io::Write;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::os::unix::process::CommandExt;

const FALLBACK_LOG_MAX_BYTES: u64 = 1024 * 1024;

/// ``jsonl_util.rotate_jsonl_at``: non-blocking flock on ``<name>.lock``.
fn rotate(path: &std::path::Path) {
    use std::os::fd::AsRawFd;
    let lock_path = path.with_file_name(format!(
        "{}.lock",
        path.file_name().unwrap_or_default().to_string_lossy()
    ));
    let Ok(lock) = std::fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .mode(0o600)
        .open(&lock_path)
    else {
        return;
    };
    let fd = lock.as_raw_fd();
    if unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        return;
    }
    if std::fs::metadata(path)
        .map(|m| m.len() >= FALLBACK_LOG_MAX_BYTES)
        .unwrap_or(false)
    {
        let dst = path.with_file_name(format!(
            "{}.1",
            path.file_name().unwrap_or_default().to_string_lossy()
        ));
        let _ = std::fs::rename(path, dst);
    }
    unsafe { libc::flock(fd, libc::LOCK_UN) };
}

/// Append one record to ``$KIROCREW_HOME/logs/stub_fallback.jsonl``.
/// Every error is swallowed: logging must never block the exec.
pub fn log_fallback(reason: &str, stub_uuid: &str, pool_label: &str, a: &Args, terminal: bool) {
    let dir = crew_home().join("logs");
    if std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(&dir)
        .is_err()
    {
        return;
    }
    let path = dir.join("stub_fallback.jsonl");
    let ts = now_f64();
    let rec = json!({
        "ts": ts,
        "ts_iso": iso_utc(ts),
        "pid": std::process::id(),
        "stub_uuid": stub_uuid,
        "pool_label": pool_label,
        "reason": reason,
        "terminal": terminal,
        "server": a.server,
        "agent": a.agent,
        "channel_id": a.channel_id.clone().unwrap_or_default(),
        "target_command": a.target_command,
    });
    rotate(&path);
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .open(&path)
    {
        let mut line = serde_json::to_vec(&rec).unwrap_or_default();
        line.push(b'\n');
        let _ = f.write_all(&line);
    }
}

/// ``_keep_socket_across_exec``: clear FD_CLOEXEC so the daemon sees this
/// connection until the exec'd backend exits.
pub fn keep_fd_across_exec(fd: i32) {
    unsafe {
        let flags = libc::fcntl(fd, libc::F_GETFD);
        if flags >= 0 {
            libc::fcntl(fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC);
        }
    }
}

/// Exit status of a fallback refusing a ``secret://`` env (``EX_CONFIG``), as
/// ``stub._EXIT_UNRESOLVED_SECRET``.
const EXIT_UNRESOLVED_SECRET: i32 = 78;

/// ``stub._refuse_unresolved_secret_references``: a direct exec cannot read
/// the vault, so a declared ``secret://`` value would start the server with
/// the literal reference as its credential. Names keys only, never a value.
fn refuse_unresolved_secret_references(
    a: &Args,
    declared: &std::collections::BTreeMap<String, String>,
) {
    // BTreeMap iterates in key order: the same sorted list Python names.
    let keys: Vec<&str> = declared
        .iter()
        .filter(|(_, v)| v.starts_with("secret://"))
        .map(|(k, _)| k.as_str())
        .collect();
    if keys.is_empty() {
        return;
    }
    let server = if a.server.is_empty() { "?" } else { &a.server };
    let names: Vec<String> = keys.iter().map(|k| format!("'{k}'")).collect();
    let message = format!(
        "MCP server '{server}' was not started: env {} holds a secret:// reference, which is \
         resolved only when the gateway starts the server, and this session fell back to \
         starting it directly. Start a new session once the gateway is serving it; \
         `kirocrew doctor` reports the gateway's state.",
        names.join(", ")
    );
    log!(ERROR, "fallback: {message}");
    eprintln!("{message}");
    std::process::exit(EXIT_UNRESOLVED_SECRET);
}

/// Replace this process with the real backend. Never returns on success.
///
/// The Python stub's ``fallback_exec`` runs it when the overlay recorded an
/// interpreter (``handoff::run_python_fallback``): it refuses an unresolvable
/// ``secret://`` env and keeps the session token for Kiro Crew's own control
/// plane after gatewayd's ``_spawns_own_control_plane`` vetting. Only when that
/// hand-over is impossible does the direct exec below run; it refuses
/// ``secret://`` the same way but ALWAYS drops the token, because the vetting
/// is not ported and its documented failure mode is "not ours".
pub fn fallback_exec(a: &Args) -> ! {
    crate::handoff::run_python_fallback(a);
    let target_args = a.target_args();
    let declared = parse_env_file(&a.env_file);
    refuse_unresolved_secret_references(a, &declared);
    let mut env: Vec<(std::ffi::OsString, std::ffi::OsString)> = std::env::vars_os().collect();
    for (k, v) in declared {
        env.retain(|(ek, _)| ek != k.as_str());
        env.push((k.into(), v.into()));
    }
    env.retain(|(k, _)| k != STUB_SESSION_TOKEN_ENV);
    if crate::payload::KIROCREW_BIN_MCP_SERVERS.contains(&a.server.as_str())
        && std::env::var_os(STUB_SESSION_TOKEN_ENV).is_some()
    {
        log!(
            WARNING,
            "fallback: {:?} is a control-plane name; the Rust stub does not run gatewayd's \
             control-plane vetting, so the direct backend runs without the session token",
            a.server
        );
    }
    let err = std::process::Command::new(&a.target_command)
        .args(&target_args)
        .env_clear()
        .envs(env)
        .exec();
    log!(ERROR, "execvpe({:?}) failed: {err}", a.target_command);
    std::process::exit(1);
}
