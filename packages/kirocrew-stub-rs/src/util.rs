//! Logging to stderr in the Python stub's line shape, plus small helpers.

use std::sync::atomic::{AtomicU8, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

pub const DEBUG: u8 = 10;
pub const INFO: u8 = 20;
pub const WARNING: u8 = 30;
pub const ERROR: u8 = 40;

static LEVEL: AtomicU8 = AtomicU8::new(WARNING);

/// Same knob and same "an unknown level means WARNING" rule as the Python stub.
pub fn init_from_env() {
    let raw = std::env::var("KIROCREW_MCP_LOG")
        .or_else(|_| std::env::var("MC_MCP_LOG"))
        .unwrap_or_else(|_| "warning".into())
        .to_ascii_uppercase();
    let lvl = match raw.as_str() {
        "DEBUG" => DEBUG,
        "INFO" => INFO,
        "WARNING" | "WARN" => WARNING,
        "ERROR" => ERROR,
        "CRITICAL" | "FATAL" => 50,
        _ => WARNING,
    };
    LEVEL.store(lvl, Ordering::Relaxed);
}

pub fn enabled(level: u8) -> bool {
    level >= LEVEL.load(Ordering::Relaxed)
}

pub fn emit(level: u8, msg: &str) {
    if !enabled(level) {
        return;
    }
    let name = match level {
        DEBUG => "DEBUG",
        INFO => "INFO",
        WARNING => "WARNING",
        ERROR => "ERROR",
        _ => "CRITICAL",
    };
    let (date, time, ms) = utc_parts(now_f64());
    eprintln!("{date} {time},{ms:03} {name} kiro_crew.mcp_gateway.stub {msg}");
}

#[macro_export]
macro_rules! log {
    ($lvl:expr, $($arg:tt)*) => {
        if $crate::util::enabled($lvl) { $crate::util::emit($lvl, &format!($($arg)*)); }
    };
}

pub fn now_f64() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// Civil date from days since 1970-01-01 (Howard Hinnant's algorithm).
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

fn utc_parts(ts: f64) -> (String, String, u32) {
    let secs = ts.floor() as i64;
    let ms = ((ts - secs as f64) * 1000.0) as u32;
    let days = secs.div_euclid(86_400);
    let rem = secs.rem_euclid(86_400);
    let (y, m, d) = civil_from_days(days);
    (
        format!("{y:04}-{m:02}-{d:02}"),
        format!("{:02}:{:02}:{:02}", rem / 3600, (rem % 3600) / 60, rem % 60),
        ms,
    )
}

/// ``time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())``.
pub fn iso_utc(ts: f64) -> String {
    let (date, time, _) = utc_parts(ts);
    format!("{date}T{time}Z")
}

pub fn sha256_hex(data: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    let d = Sha256::digest(data);
    hex(&d)
}

pub fn hex(bytes: &[u8]) -> String {
    const T: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push(T[(b >> 4) as usize] as char);
        s.push(T[(b & 15) as usize] as char);
    }
    s
}

/// Python ``str.strip()`` with no argument: Unicode whitespace plus the
/// ASCII separators (0x1c-0x1f) Python also counts.
pub fn py_strip(s: &str) -> &str {
    s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn iso_matches_known_instant() {
        // 2026-10-10T17:57:26Z
        assert_eq!(iso_utc(1_791_655_046.4), "2026-10-10T17:57:26Z");
        assert_eq!(iso_utc(0.0), "1970-01-01T00:00:00Z");
    }

    #[test]
    fn strip_matches_python() {
        assert_eq!(py_strip("\x1e{\"a\":1}\x0c\n"), "{\"a\":1}");
    }
}
