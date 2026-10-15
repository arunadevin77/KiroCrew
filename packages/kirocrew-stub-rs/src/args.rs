//! Argv: the ``--stub-flags-b64`` envelope plus the argparse surface of
//! ``kiro_crew.mcp_gateway.stub._parse_args``.

use base64::Engine;
use base64::engine::{DecodePaddingMode, GeneralPurpose, GeneralPurposeConfig};
use std::path::PathBuf;

pub const STUB_FLAGS_FLAG: &str = "--stub-flags-b64";

const B64URL: GeneralPurpose = GeneralPurpose::new(
    &base64::alphabet::URL_SAFE,
    GeneralPurposeConfig::new().with_decode_padding_mode(DecodePaddingMode::Indifferent),
);

/// ``hashing.decode_target_args``: base64url JSON array of strings.
pub fn decode_b64_json_list(raw: &str) -> Result<Vec<String>, String> {
    let bytes = B64URL
        .decode(raw.as_bytes())
        .map_err(|_| "malformed target-args payload".to_string())?;
    let v: serde_json::Value =
        serde_json::from_slice(&bytes).map_err(|_| "malformed target-args payload".to_string())?;
    let arr = v
        .as_array()
        .ok_or_else(|| "target-args payload is not a JSON array of strings".to_string())?;
    arr.iter()
        .map(|x| {
            x.as_str()
                .map(str::to_owned)
                .ok_or_else(|| "target-args payload is not a JSON array of strings".to_string())
        })
        .collect()
}

pub fn decode_b64_json(raw: &str) -> Option<serde_json::Value> {
    let bytes = B64URL.decode(raw.as_bytes()).ok()?;
    serde_json::from_slice(&bytes).ok()
}

/// ``hashing.expand_stub_flags``: one level of envelope splicing.
pub fn expand_stub_flags(argv: &[String]) -> Result<Vec<String>, String> {
    let mut out = Vec::with_capacity(argv.len());
    let mut i = 0;
    while i < argv.len() {
        let tok = &argv[i];
        if tok == STUB_FLAGS_FLAG {
            i += 1;
            let payload = argv
                .get(i)
                .ok_or_else(|| "stub-flags envelope has no payload".to_string())?;
            out.extend(decode_b64_json_list(payload)?);
        } else if let Some(payload) = tok.strip_prefix(&format!("{STUB_FLAGS_FLAG}=")) {
            out.extend(decode_b64_json_list(payload)?);
        } else {
            out.push(tok.clone());
        }
        i += 1;
    }
    Ok(out)
}

#[derive(Debug, Clone, Default)]
pub struct Args {
    pub server: String,
    pub agent: String,
    pub target_command: String,
    pub target_args_b64: Option<String>,
    pub target_args: String,
    pub target_args_sep: String,
    pub sandbox_mode: String,
    pub work_dir: String,
    pub env: String,
    pub env_json: String,
    pub env_file: String,
    pub auto_approve: String,
    pub approval_mode: String,
    pub trust_all: bool,
    pub poolable: bool,
    pub channel_id: Option<String>,
    pub pool_identity_env_b64: Option<String>,
    pub pool_identity_env: String,
    pub socket: String,
    pub read_limit: Option<String>,
    /// Rust-only: the gateway's own install facts the Python stub reads from
    /// its interpreter (``sys.executable``, the package directory). See
    /// ``rewriter._rust_install_context``.
    pub install_context_b64: Option<String>,
    /// The expanded flags minus the Rust-only ``--install-context-b64``: what a
    /// Python stub launched from the same overlay would have parsed. Used to
    /// hand a stub over to the Python implementation (``handoff``).
    pub python_flags: Vec<String>,
}

/// ``config.paths.config_dir()``'s choice of home: ``KIROCREW_HOME`` only when
/// ``_valid_override_home`` accepts it (expanded, resolved, and not a root or a
/// system directory), else the default home.
pub fn crew_home() -> PathBuf {
    if let Ok(h) = std::env::var("KIROCREW_HOME")
        && !h.is_empty()
    {
        let p = PathBuf::from(crate::payload::resolve_path(&expand_user(&h)));
        if !is_unsafe_home(&p) {
            return p;
        }
        crate::log!(
            crate::util::WARNING,
            "KIROCREW_HOME={h:?} is a root or system directory; using the default home"
        );
    }
    let base = std::env::var("HOME").unwrap_or_else(|_| ".".into());
    PathBuf::from(base).join(".kiro").join("crew")
}

/// ``Path.expanduser`` for the current user's ``~`` (``~user`` is left as is).
fn expand_user(p: &str) -> String {
    let home = || std::env::var("HOME").ok().filter(|h| !h.is_empty());
    if p == "~" {
        return home().unwrap_or_else(|| p.into());
    }
    if let Some(rest) = p.strip_prefix("~/")
        && let Some(h) = home()
    {
        return format!("{}/{rest}", h.trim_end_matches('/'));
    }
    p.into()
}

/// ``config.paths._is_unsafe_home`` on a resolved path.
pub fn is_unsafe_home(p: &std::path::Path) -> bool {
    if p.parent().is_none() {
        return true;
    }
    let parts: Vec<String> = p
        .components()
        .take(3)
        .map(|c| c.as_os_str().to_string_lossy().into_owned())
        .collect();
    let two: Vec<&str> = parts.iter().take(2).map(String::as_str).collect();
    matches!(
        two.as_slice(),
        ["/", "usr"] | ["/", "System"] | ["/", "etc"]
    ) || parts.iter().map(String::as_str).collect::<Vec<_>>() == ["/", "private", "etc"]
}

fn default_socket_path() -> String {
    let home = crew_home();
    let new_path = home.join("kirocrew-mcp-gateway.sock");
    let legacy = home.join("mc-mcp-gateway.sock");
    if !new_path.exists() && legacy.exists() {
        return legacy.to_string_lossy().into_owned();
    }
    new_path.to_string_lossy().into_owned()
}

fn usage_error(msg: &str) -> ! {
    eprintln!(
        "usage: kirocrew-mcp-stub [-h] --server SERVER --agent AGENT --target-command TARGET_COMMAND --work-dir WORK_DIR [options]"
    );
    eprintln!("kirocrew-mcp-stub: error: {msg}");
    std::process::exit(2);
}

pub fn parse(raw: &[String]) -> Args {
    let argv = match expand_stub_flags(raw) {
        Ok(a) => a,
        // argparse never sees a malformed envelope: ``expand_stub_flags``
        // raises ValueError before it, which kills the Python stub (exit 1).
        Err(e) => {
            eprintln!("ValueError: {e}");
            std::process::exit(1);
        }
    };
    let mut a = Args {
        target_args_sep: "|".into(),
        sandbox_mode: "standard".into(),
        approval_mode: "interactive".into(),
        ..Default::default()
    };
    let mut socket: Option<String> = None;
    let (mut has_server, mut has_agent, mut has_cmd, mut has_wd) = (false, false, false, false);
    let mut i = 0;
    while i < argv.len() {
        let tok = argv[i].clone();
        let (flag, inline) = match tok.split_once('=') {
            Some((f, v)) if f.starts_with("--") => (f.to_string(), Some(v.to_string())),
            _ => (tok.clone(), None),
        };
        match flag.as_str() {
            "--trust-all" | "--poolable" => {
                if inline.is_some() {
                    usage_error(&format!("argument {flag}: ignored explicit argument"));
                }
                if flag == "--poolable" {
                    a.poolable = true;
                } else {
                    a.trust_all = true;
                }
                i += 1;
                continue;
            }
            "-h" | "--help" => {
                eprintln!("usage: kirocrew-mcp-stub (Rust port of kiro_crew.mcp_gateway.stub)");
                std::process::exit(0);
            }
            _ => {}
        }
        let known = matches!(
            flag.as_str(),
            "--server"
                | "--agent"
                | "--target-command"
                | "--target-args-b64"
                | "--target-args"
                | "--target-args-sep"
                | "--sandbox-mode"
                | "--work-dir"
                | "--env"
                | "--env-json"
                | "--env-file"
                | "--auto-approve"
                | "--approval-mode"
                | "--channel-id"
                | "--pool-identity-env-b64"
                | "--pool-identity-env"
                | "--socket"
                | "--real-stub"
                | "--read-limit"
                | "--install-context-b64"
        );
        if !known {
            usage_error(&format!("unrecognized arguments: {tok}"));
        }
        let value = match inline {
            Some(v) => v,
            None => {
                i += 1;
                match argv.get(i) {
                    // argparse refuses a following token that looks like an
                    // option (and is not a negative number).
                    Some(v)
                        if !(v.starts_with('-') && v.len() > 1 && v.parse::<f64>().is_err()) =>
                    {
                        v.clone()
                    }
                    _ => usage_error(&format!("argument {flag}: expected one argument")),
                }
            }
        };
        match flag.as_str() {
            "--server" => {
                a.server = value;
                has_server = true
            }
            "--agent" => {
                a.agent = value;
                has_agent = true
            }
            "--target-command" => {
                a.target_command = value;
                has_cmd = true
            }
            "--target-args-b64" => a.target_args_b64 = Some(value),
            "--target-args" => a.target_args = value,
            "--target-args-sep" => a.target_args_sep = value,
            "--sandbox-mode" => a.sandbox_mode = value,
            "--work-dir" => {
                a.work_dir = value;
                has_wd = true
            }
            "--env" => a.env = value,
            "--env-json" => a.env_json = value,
            "--env-file" => a.env_file = value,
            "--auto-approve" => a.auto_approve = value,
            "--approval-mode" => a.approval_mode = value,
            "--channel-id" => a.channel_id = Some(value),
            "--pool-identity-env-b64" => a.pool_identity_env_b64 = Some(value),
            "--pool-identity-env" => a.pool_identity_env = value,
            "--socket" => socket = Some(value),
            "--real-stub" => {}
            "--read-limit" => a.read_limit = Some(value),
            "--install-context-b64" => a.install_context_b64 = Some(value),
            _ => unreachable!(),
        }
        i += 1;
    }
    let mut missing = Vec::new();
    if !has_server {
        missing.push("--server");
    }
    if !has_agent {
        missing.push("--agent");
    }
    if !has_cmd {
        missing.push("--target-command");
    }
    if !has_wd {
        missing.push("--work-dir");
    }
    if !missing.is_empty() {
        usage_error(&format!(
            "the following arguments are required: {}",
            missing.join(", ")
        ));
    }
    a.socket = socket.unwrap_or_else(|| {
        std::env::var("KIROCREW_MCP_SOCKET")
            .ok()
            .filter(|s| !s.is_empty())
            .or_else(|| {
                std::env::var("MC_MCP_SOCKET")
                    .ok()
                    .filter(|s| !s.is_empty())
            })
            .unwrap_or_else(default_socket_path)
    });
    a.python_flags = strip_install_context(&argv);
    a
}

/// ``argv`` without ``--install-context-b64`` and its value (either spelling).
/// Only called after ``parse`` accepted ``argv``, so a bare flag always has
/// its value next to it.
pub fn strip_install_context(argv: &[String]) -> Vec<String> {
    let mut out = Vec::with_capacity(argv.len());
    let mut i = 0;
    while i < argv.len() {
        let tok = &argv[i];
        if tok == "--install-context-b64" {
            i += 2;
            continue;
        }
        if !tok.starts_with("--install-context-b64=") {
            out.push(tok.clone());
        }
        i += 1;
    }
    out
}

/// ``hashing.encode_target_args``: base64url (padded) JSON array of strings.
pub fn encode_b64_json_list(items: &[String]) -> String {
    let raw = serde_json::to_vec(items).unwrap_or_else(|_| b"[]".to_vec());
    base64::engine::general_purpose::URL_SAFE.encode(raw)
}

impl Args {
    /// ``_resolve_target_args``.
    pub fn target_args(&self) -> Vec<String> {
        if let Some(enc) = &self.target_args_b64 {
            return match decode_b64_json_list(enc) {
                Ok(v) => v,
                Err(e) => {
                    // Python raises ValueError out of build_register_payload.
                    eprintln!("ValueError: {e}");
                    std::process::exit(1);
                }
            };
        }
        split_sep(&self.target_args, &self.target_args_sep)
    }

    /// ``_resolve_pool_identity_env``.
    pub fn pool_identity_env(&self) -> Vec<String> {
        let names = match &self.pool_identity_env_b64 {
            Some(enc) => decode_b64_json_list(enc).unwrap_or_default(),
            None => split_sep(&self.pool_identity_env, &self.target_args_sep),
        };
        names.into_iter().filter(|n| !n.is_empty()).collect()
    }
}

fn split_sep(raw: &str, sep: &str) -> Vec<String> {
    if raw.is_empty() {
        return Vec::new();
    }
    if sep.is_empty() {
        // Python's str.split("") raises ValueError.
        eprintln!("ValueError: empty separator");
        std::process::exit(1);
    }
    raw.split(sep).map(str::to_owned).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unsafe_homes_match_python() {
        // config.paths._is_unsafe_home: roots and system trees are refused.
        for p in [
            "/",
            "/usr",
            "/usr/local/x",
            "/etc",
            "/etc/kirocrew",
            "/System/x",
            "/private/etc/x",
        ] {
            assert!(is_unsafe_home(std::path::Path::new(p)), "{p}");
        }
        for p in [
            "/home/u/.kiro/crew",
            "/tmp/h",
            "/private/var/folders/x",
            "/etcetera",
        ] {
            assert!(!is_unsafe_home(std::path::Path::new(p)), "{p}");
        }
    }

    #[test]
    fn python_flags_drop_only_the_install_context() {
        let argv: Vec<String> = [
            "--server",
            "s",
            "--install-context-b64",
            "CTX",
            "--agent",
            "a",
            "--install-context-b64=CTX2",
            "--poolable",
        ]
        .map(String::from)
        .to_vec();
        assert_eq!(
            strip_install_context(&argv),
            ["--server", "s", "--agent", "a", "--poolable"].map(String::from)
        );
        // Round-trips through the envelope the Python stub expands.
        let flags = strip_install_context(&argv);
        let env = format!("{STUB_FLAGS_FLAG}={}", encode_b64_json_list(&flags));
        assert_eq!(expand_stub_flags(&[env]).unwrap(), flags);
    }

    fn enc(v: &[&str]) -> String {
        B64URL.encode(serde_json::to_vec(&v).unwrap())
    }

    #[test]
    fn envelope_splices_both_spellings() {
        let raw = vec![
            format!("{STUB_FLAGS_FLAG}={}", enc(&["--server", "s"])),
            STUB_FLAGS_FLAG.to_string(),
            enc(&["--agent", "a"]),
            "--poolable".into(),
        ];
        assert_eq!(
            expand_stub_flags(&raw).unwrap(),
            vec!["--server", "s", "--agent", "a", "--poolable"]
        );
    }

    #[test]
    fn parses_live_shape() {
        let raw = vec![format!(
            "{STUB_FLAGS_FLAG}={}",
            enc(&[
                "--server",
                "kirocrew-cron",
                "--agent",
                "kirocrew",
                "--target-command",
                "/x/kirocrew",
                "--target-args-b64=WyJtY3AtY3JvbiJd",
                "--sandbox-mode",
                "off",
                "--work-dir",
                "/w",
                "--approval-mode",
                "auto",
                "--socket",
                "/s.sock",
                "--read-limit",
                "67108864",
                "--poolable",
            ])
        )];
        let a = parse(&raw);
        assert_eq!(a.server, "kirocrew-cron");
        assert_eq!(a.target_args(), vec!["mcp-cron"]);
        assert!(a.poolable);
        assert_eq!(a.socket, "/s.sock");
        assert_eq!(a.read_limit.as_deref(), Some("67108864"));
    }
}
