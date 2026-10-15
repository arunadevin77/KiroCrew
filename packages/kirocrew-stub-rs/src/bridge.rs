//! Gateway protocol: handshake, admission, the stdio<->socket bridge with its
//! liveness monitor, and the reconnect that outlives a gateway restart.
//!
//! Each function mirrors the Python function of the same name in
//! ``kiro_crew/mcp_gateway/stub.py``; constants keep their Python values.

use crate::io::{
    Input, Obj, ReadErr, SockReader, SockWriter, frame_bytes, id_key, is_json_non_object,
    parse_obj, parse_obj_lossy, sock_write, stdout, write_frame,
};
use crate::log;
use crate::util::{INFO, WARNING};
use serde_json::{Value, json};
use std::cell::{Cell, RefCell};
use std::collections::BTreeMap;
use std::rc::Rc;
use std::time::{Duration, Instant};
use tokio::net::UnixStream;
use tokio::sync::{Mutex, Notify, watch};

pub const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(3);
pub const ERROR_EMIT_TIMEOUT: Duration = Duration::from_secs(5);
const REPLAY_INIT_TIMEOUT: Duration = Duration::from_secs(30);
pub const SPAWN_QUEUE_SILENCE: Duration = Duration::from_secs(25);
const RECONNECT_BACKOFF_START: f64 = 0.5;
const RECONNECT_BACKOFF_MAX: f64 = 10.0;
pub const CAPACITY_ERROR_CODE: i64 = -32001;
const QUEUED_FAIL_MESSAGE: &str =
    "Gateway restarted and is not back yet; this call was failed so it would not hang. Retry it.";

fn env_secs(name: &str, default: f64) -> Duration {
    // Test seam only: lets the conformance suite exercise the 10 s ping and
    // 600 s reconnect paths without waiting them out.
    let v = std::env::var(name)
        .ok()
        .and_then(|s| s.parse::<f64>().ok())
        .unwrap_or(default);
    Duration::from_secs_f64(v.max(0.001))
}
pub fn ping_interval() -> Duration {
    env_secs("KIROCREW_STUB_RS_PING_INTERVAL", 10.0)
}
pub const PING_MAX_MISSES: u32 = 3;
pub fn reconnect_budget() -> Duration {
    env_secs("KIROCREW_STUB_RS_RECONNECT_BUDGET", 600.0)
}
fn queued_grace() -> Duration {
    ping_interval() * PING_MAX_MISSES
}

// ---------------------------------------------------------------------------
// Stop signal (SIGTERM/SIGINT).
// ---------------------------------------------------------------------------

#[derive(Clone)]
pub struct Stop(pub watch::Receiver<bool>);

impl Stop {
    pub fn is_set(&self) -> bool {
        *self.0.borrow()
    }
    pub async fn wait(&self) {
        let mut rx = self.0.clone();
        let _ = rx.wait_for(|v| *v).await;
        if !*rx.borrow() {
            std::future::pending::<()>().await;
        }
    }
    /// ``asyncio.wait_for(stop_event.wait(), timeout=d)``: true when stopped.
    pub async fn wait_timeout(&self, d: Duration) -> bool {
        tokio::time::timeout(d, self.wait()).await.is_ok()
    }
}

// ---------------------------------------------------------------------------
// Read ceiling (``_adopt_read_ceiling``).
// ---------------------------------------------------------------------------

pub const DEFAULT_READ_LIMIT: usize = 64 * 1024 * 1024;

#[derive(Clone, Copy)]
pub struct Limits {
    pub read: usize,
    pub held_frame: usize,
    pub held_total: usize,
}

impl Limits {
    pub fn adopt(flag: Option<&str>) -> Self {
        let env = std::env::var("KIROCREW_MCP_READ_LIMIT")
            .ok()
            .and_then(|s| s.trim().parse::<i64>().ok())
            .filter(|v| *v >= 1024);
        let flag_v = flag.and_then(|s| s.trim().parse::<i64>().ok());
        if flag.is_some() && flag_v.is_none() {
            log!(
                WARNING,
                "ignoring unparsable --read-limit {:?}",
                flag.unwrap_or("")
            );
        }
        let v = env
            .or(flag_v.filter(|v| *v >= 1024))
            .or_else(config_read_limit)
            .unwrap_or(DEFAULT_READ_LIMIT as i64) as usize;
        Self {
            read: v,
            held_frame: v,
            held_total: v.max(DEFAULT_READ_LIMIT),
        }
    }
}

/// ``read_limits.config_read_buffer_limit``, reading the data home's config.json.
fn config_read_limit() -> Option<i64> {
    let raw = std::fs::read_to_string(crate::args::crew_home().join("config.json")).ok()?;
    let v: Value = serde_json::from_str(raw.trim_start_matches('\u{feff}')).ok()?;
    v.get("mcp_gateway")?
        .get("read_buffer_limit_bytes")?
        .as_i64()
        .filter(|n| *n >= 1024)
}

// ---------------------------------------------------------------------------
// Session state that outlives one connection (``StubSession``).
// ---------------------------------------------------------------------------

#[derive(Default)]
pub struct Meta {
    pub reason: String,
    pub outstanding: Vec<Value>,
    pub captured_init: Option<Vec<u8>>,
    pub init_result: Option<Value>,
    pub reconnects: u32,
    subscribed: bool,
}

pub const RECONNECTABLE: &[&str] = &["peer_dead", "socket_eof", "socket_write_failed"];

impl Meta {
    fn note_outbound(&mut self, line: &[u8], msg: &Obj) {
        let method = msg.get("method").and_then(Value::as_str);
        if self.captured_init.is_none() && method == Some("initialize") {
            let mut l = line.to_vec();
            if !l.ends_with(b"\n") {
                l.push(b'\n');
            }
            self.captured_init = Some(l);
        } else if method == Some("resources/subscribe") {
            self.subscribed = true;
        }
    }
    pub fn has_live_subscriptions(&self) -> bool {
        self.subscribed
    }
    pub fn note_init_result(&mut self, msg: &Obj) {
        if self.init_result.is_none()
            && let Some(r @ Value::Object(_)) = msg.get("result")
        {
            self.init_result = Some(r.clone());
        }
    }
    pub fn captured_init_id(&self) -> Option<Value> {
        let l = self.captured_init.as_ref()?;
        serde_json::from_slice::<Value>(l).ok()?.get("id").cloned()
    }
}

// ---------------------------------------------------------------------------
// Frame reads with the Python error taxonomy.
// ---------------------------------------------------------------------------

pub enum FrameErr {
    /// ``_NonObjectFrame``: valid JSON, not an object.
    NonObject,
    /// ``json.JSONDecodeError``.
    Decode,
}

/// ``stub._read_frame``: Ok(None) is "closed".
pub async fn read_frame(r: &mut SockReader) -> Result<Option<Obj>, FrameErr> {
    let line = match r.read_line().await {
        Ok(l) => l,
        Err(ReadErr::Closed) => return Ok(None),
    };
    if let Some(m) = parse_obj_lossy(&line) {
        return Ok(Some(m));
    }
    if is_json_non_object(&line) {
        return Err(FrameErr::NonObject);
    }
    Err(FrameErr::Decode)
}

pub fn caps_of(registered: &Obj) -> Vec<String> {
    registered
        .get("capabilities")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|v| v.as_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default()
}

pub fn has_cap(caps: &[String], c: &str) -> bool {
    caps.iter().any(|x| x == c)
}

pub fn must_degrade_unshareable(poolable: bool, caps: &[String]) -> bool {
    !poolable && !has_cap(caps, "poolable_ack")
}

pub fn must_degrade_nonce_blind(server: &str, poolable: bool, caps: &[String]) -> bool {
    crate::payload::POOLING_REQUIRES_TENANT_NONCE.contains(&server)
        && poolable
        && !has_cap(caps, "tenant_nonce")
}

pub fn must_degrade_unknown_target(reason: &str) -> bool {
    reason.contains("no target mapping for server")
}

// ---------------------------------------------------------------------------
// Handshake.
// ---------------------------------------------------------------------------

pub enum HsErr {
    Fallback(String),
    /// ``StaleGenerationError``.
    Stale(String),
}

pub struct Conn {
    pub reader: SockReader,
    pub writer: SockWriter,
    pub fd: i32,
}

pub async fn close(w: &SockWriter) {
    use tokio::io::AsyncWriteExt;
    let mut g = w.lock().await;
    let _ = g.shutdown().await;
}

/// ``stub.handshake``: connect + Register.
pub async fn handshake(socket: &str, payload: &Obj, limit: usize) -> Result<(Conn, Obj), HsErr> {
    use std::os::fd::AsRawFd;
    let stream = UnixStream::connect(socket)
        .await
        .map_err(|e| HsErr::Fallback(format!("connect failed: {e}")))?;
    let fd = stream.as_raw_fd();
    let (r, w) = stream.into_split();
    let mut reader = SockReader::new(r, limit);
    let writer: SockWriter = Rc::new(Mutex::new(w));
    if let Err(e) = write_frame(&writer, payload).await {
        close(&writer).await;
        return Err(HsErr::Fallback(format!("register io failed: {e}")));
    }
    let resp = match read_frame(&mut reader).await {
        Ok(Some(m)) => m,
        Ok(None) => {
            close(&writer).await;
            return Err(HsErr::Fallback("gateway closed during handshake".into()));
        }
        Err(_) => {
            close(&writer).await;
            return Err(HsErr::Fallback(
                "register io failed: gateway frame is not JSON".into(),
            ));
        }
    };
    let t = resp.get("type").and_then(Value::as_str).unwrap_or("");
    if t == "registered" {
        if let Some(expected) = payload.get("stub_code_fingerprint").and_then(Value::as_str)
            && !expected.is_empty()
        {
            match resp.get("fingerprint").and_then(Value::as_str) {
                Some(d) if !d.is_empty() => {
                    if d != expected {
                        close(&writer).await;
                        return Err(HsErr::Stale(
                                "gateway code fingerprint does not match this stub; using direct execution".into(),
                            ));
                    }
                }
                _ => {
                    close(&writer).await;
                    return Err(HsErr::Stale(
                        "gateway did not report its code fingerprint; using direct execution"
                            .into(),
                    ));
                }
            }
        }
        return Ok((Conn { reader, writer, fd }, resp));
    }
    close(&writer).await;
    if t == "rejected" {
        let reason = match resp.get("reason") {
            Some(Value::String(s)) => s.clone(),
            Some(v) => v.to_string(),
            None => "?".into(),
        };
        return Err(HsErr::Fallback(format!("gateway rejected: {reason}")));
    }
    Err(HsErr::Fallback(format!(
        "unexpected handshake reply: type={}",
        resp.get("type")
            .map(|v| v.to_string())
            .unwrap_or_else(|| "None".into())
    )))
}

// ---------------------------------------------------------------------------
// Admission pre-flight (``_ensure_backend_admitted``).
// ---------------------------------------------------------------------------

#[derive(PartialEq, Debug)]
pub enum Admit {
    Ready,
    Rejected,
    Timeout,
    Closed,
}

fn admission_control(msg: &Obj) -> bool {
    matches!(
        msg.get("type").and_then(Value::as_str),
        Some("queued" | "keepalive" | "pong")
    )
}

pub async fn ensure_backend_admitted(
    conn: &mut Conn,
    queue_aware: bool,
    total_budget: Duration,
) -> (Admit, Option<Obj>) {
    let mut frame = Obj::new();
    frame.insert("type".into(), json!("ensure_backend"));
    if queue_aware {
        frame.insert("wait_budget_secs".into(), json!(total_budget.as_secs_f64()));
    }
    if write_frame(&conn.writer, &frame).await.is_err() {
        return (Admit::Closed, None);
    }
    let deadline = Instant::now()
        + if queue_aware {
            total_budget
        } else {
            SPAWN_QUEUE_SILENCE
        };
    loop {
        let now = Instant::now();
        if now >= deadline {
            return (Admit::Timeout, None);
        }
        let wait = SPAWN_QUEUE_SILENCE.min(deadline - now);
        let msg = match tokio::time::timeout(wait, read_frame(&mut conn.reader)).await {
            Err(_) => return (Admit::Timeout, None),
            Ok(Err(FrameErr::NonObject)) => continue,
            Ok(Err(FrameErr::Decode)) | Ok(Ok(None)) => return (Admit::Closed, None),
            Ok(Ok(Some(m))) => m,
        };
        if queue_aware && admission_control(&msg) {
            continue;
        }
        match msg.get("type").and_then(Value::as_str) {
            Some("ready") => return (Admit::Ready, Some(msg)),
            Some("rejected") => return (Admit::Rejected, Some(msg)),
            _ if !queue_aware => return (Admit::Rejected, Some(msg)),
            _ => {}
        }
    }
}

pub fn rejection_class(frame: &Obj) -> Option<String> {
    frame
        .get("class")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
}

// ---------------------------------------------------------------------------
// Error frames.
// ---------------------------------------------------------------------------

/// ``_emit_error_frames``: one -32603 per id, bounded as a whole.
pub async fn emit_error_frames(ids: &[Value], message: &str, label: &str) {
    if ids.is_empty() {
        return;
    }
    let mut buf = Vec::new();
    for id in ids {
        let mut f = Obj::new();
        f.insert("jsonrpc".into(), json!("2.0"));
        f.insert("id".into(), id.clone());
        f.insert("error".into(), json!({"code": -32603, "message": message}));
        buf.extend(frame_bytes(&f));
    }
    if !stdout().emit_bounded(buf, ERROR_EMIT_TIMEOUT).await {
        log!(WARNING, "could not emit error frames pool={label}");
    }
}

// ---------------------------------------------------------------------------
// The bridge.
// ---------------------------------------------------------------------------

fn set_once(cell: &RefCell<String>, v: &str) {
    let mut r = cell.borrow_mut();
    if r.is_empty() {
        *r = v.to_string();
    }
}

/// ``stub.run_bridge``. Reports through ``meta.reason`` / ``meta.outstanding``.
#[allow(clippy::too_many_arguments)]
pub async fn run_bridge(
    mut conn: Conn,
    stop: &Stop,
    input: &mut Input,
    meta: &mut Meta,
    peer_supports_ping: bool,
    recaller: Option<Option<String>>,
) {
    meta.reason.clear();
    meta.outstanding.clear();
    let writer = conn.writer.clone();
    let reason = RefCell::new(String::new());
    let outstanding: RefCell<BTreeMap<String, Value>> = RefCell::new(BTreeMap::new());
    let meta_cell = RefCell::new(std::mem::take(meta));
    let pong = Cell::new(false);
    let pong_notify = Notify::new();
    let peer_dead = Cell::new(false);
    let interval = ping_interval();

    let stdin_pump = async {
        loop {
            let line = input.next_line().await;
            if line.is_empty() {
                set_once(&reason, "stdin_eof");
                let mut f = Obj::new();
                f.insert("type".into(), json!("unregister"));
                let _ = write_frame(&writer, &f).await;
                return;
            }
            if let Some(msg) = parse_obj(&line) {
                meta_cell.borrow_mut().note_outbound(&line, &msg);
                if let (true, Some(id)) = (msg.contains_key("method"), msg.get("id"))
                    && let Some(k) = id_key(id)
                {
                    outstanding.borrow_mut().insert(k, id.clone());
                }
            }
            let res = if line.ends_with(b"\n") {
                sock_write(&writer, &line).await
            } else {
                let mut l = line;
                l.push(b'\n');
                sock_write(&writer, &l).await
            };
            if res.is_err() {
                set_once(&reason, "socket_write_failed");
                return;
            }
        }
    };

    let stdout_pump = async {
        loop {
            let line = match conn.reader.read_line().await {
                Ok(l) => l,
                Err(_) => {
                    set_once(&reason, "socket_eof");
                    return;
                }
            };
            let mut control = false;
            if let Some(msg) = parse_obj(&line) {
                match msg.get("type").and_then(Value::as_str) {
                    Some("pong") | Some("queued") => {
                        pong.set(true);
                        pong_notify.notify_one();
                        control = true;
                    }
                    Some("keepalive") => control = true,
                    _ => {
                        if let (Some(id), false) = (msg.get("id"), msg.contains_key("method")) {
                            if let Some(k) = id_key(id) {
                                outstanding.borrow_mut().remove(&k);
                            }
                            let mut m = meta_cell.borrow_mut();
                            if m.captured_init_id().as_ref() == Some(id) {
                                m.note_init_result(&msg);
                            }
                        }
                    }
                }
            }
            if control {
                continue;
            }
            if stdout().emit(line).await.is_err() {
                set_once(&reason, "writer_failed");
                return;
            }
        }
    };

    let liveness = async {
        if !peer_supports_ping {
            return std::future::pending::<()>().await;
        }
        let mut misses = 0u32;
        loop {
            if outstanding.borrow().is_empty() {
                tokio::time::sleep(interval).await;
                misses = 0;
                continue;
            }
            pong.set(false);
            let mut ping = Obj::new();
            ping.insert("type".into(), json!("ping"));
            if write_frame(&writer, &ping).await.is_err() {
                return;
            }
            let sent_at = Instant::now();
            let wait_pong = async {
                while !pong.get() {
                    pong_notify.notified().await;
                }
            };
            let _ = tokio::time::timeout(interval, wait_pong).await;
            if pong.get() {
                misses = 0;
                if let Some(rem) = interval.checked_sub(sent_at.elapsed()) {
                    tokio::time::sleep(rem).await;
                }
            } else {
                misses += 1;
                log!(
                    WARNING,
                    "stub liveness: ping unanswered ({misses}/{PING_MAX_MISSES}), outstanding_ids={}",
                    outstanding.borrow().len()
                );
                if misses >= PING_MAX_MISSES {
                    log!(
                        crate::util::ERROR,
                        "stub liveness: peer dead after {misses} missed pings; triggering fallback"
                    );
                    peer_dead.set(true);
                    return;
                }
            }
        }
    };

    let recaller_fut = async {
        if let Some(channel_id) = recaller {
            recaller_loop(&writer, channel_id.as_deref(), stop).await;
        }
        std::future::pending::<()>().await
    };

    let out = stdout();
    let writer_failed = async {
        if out.failed.load(std::sync::atomic::Ordering::Acquire) {
            return;
        }
        out.failed_notify.notified().await
    };

    tokio::select! {
        _ = stdin_pump => {}
        _ = stdout_pump => {}
        _ = stop.wait() => {}
        _ = writer_failed => {}
        _ = liveness => {}
        _ = recaller_fut => {}
    }
    close(&writer).await;
    let mut m = meta_cell.into_inner();
    m.reason = if stop.is_set() {
        "stop".into()
    } else if peer_dead.get() {
        "peer_dead".into()
    } else if out.failed.load(std::sync::atomic::Ordering::Acquire) {
        "writer_failed".into()
    } else {
        let r = reason.into_inner();
        if r.is_empty() { "unknown".into() } else { r }
    };
    m.outstanding = outstanding.into_inner().into_values().collect();
    *meta = m;
}

/// ``_recaller_loop``: poll for a late session key; send ``recaller`` once.
async fn recaller_loop(writer: &SockWriter, channel_id: Option<&str>, stop: &Stop) {
    let mut interval = 1.5f64;
    loop {
        if stop.wait_timeout(Duration::from_secs_f64(interval)).await {
            return;
        }
        interval = (interval * 1.5).min(30.0);
        let caller = crate::payload::caller_block(channel_id);
        if caller["session_key"].as_str().unwrap_or("").is_empty() {
            continue;
        }
        let mut f = Obj::new();
        f.insert("type".into(), json!("recaller"));
        f.insert("session_key".into(), caller["session_key"].clone());
        f.insert("session_type".into(), caller["session_type"].clone());
        f.insert("principal_id".into(), caller["principal_id"].clone());
        f.insert("channel_id".into(), json!(channel_id));
        f.insert("caller".into(), Value::Object(caller));
        if write_frame(writer, &f).await.is_ok() {
            log!(INFO, "stub sent recaller after warm-pool claim");
        }
        return;
    }
}

// ---------------------------------------------------------------------------
// Reconnect.
// ---------------------------------------------------------------------------

#[derive(PartialEq)]
enum Replay {
    Ok,
    Retry,
    Refuse,
}

/// ``_replay_initialize``.
async fn replay_initialize(conn: &mut Conn, meta: &Meta) -> (Replay, Option<Vec<u8>>, String) {
    let Some(init) = meta.captured_init.as_ref() else {
        return (Replay::Refuse, None, "no_captured_initialize".into());
    };
    let init_id = meta.captured_init_id();
    if serde_json::from_slice::<Value>(init).is_err() {
        return (Replay::Refuse, None, "captured_initialize_malformed".into());
    }
    if let Err(e) = sock_write(&conn.writer, init).await {
        return (Replay::Retry, None, format!("replay_write_failed: {e}"));
    }
    let wait = async {
        loop {
            let line = match conn.reader.read_line().await {
                Ok(l) => l,
                Err(_) => return (Replay::Retry, None, "replay_read_failed".to_string()),
            };
            let Some(msg) = parse_obj_lossy(&line) else {
                continue;
            };
            if admission_control(&msg) {
                continue;
            }
            if msg.get("id") != init_id.as_ref() || msg.contains_key("method") {
                continue;
            }
            if let Some(err) = msg.get("error") {
                return (Replay::Refuse, None, format!("replay_rejected: {err}"));
            }
            if meta.init_result.is_none() {
                return (Replay::Ok, Some(line), "forwarded_first_answer".to_string());
            }
            if msg.get("result") != meta.init_result.as_ref() {
                return (Replay::Refuse, None, "handshake_result_changed".to_string());
            }
            return (Replay::Ok, None, "result_matched".to_string());
        }
    };
    match tokio::time::timeout(REPLAY_INIT_TIMEOUT, wait).await {
        Ok(r) => r,
        Err(_) => (Replay::Retry, None, "replay_timeout".into()),
    }
}

pub struct Attached {
    pub conn: Conn,
    pub registered: Obj,
    pub stub_uuid: String,
}

/// ``_reconnect``.
async fn reconnect(
    socket: &str,
    payload: &Obj,
    meta: &mut Meta,
    stop: &Stop,
    poolable: bool,
    label: &str,
    limit: usize,
) -> Option<Attached> {
    let started = Instant::now();
    let deadline = started + reconnect_budget();
    let mut delay = RECONNECT_BACKOFF_START;
    macro_rules! pause {
        () => {{
            if stop.wait_timeout(Duration::from_secs_f64(delay)).await {
                return None;
            }
            delay = (delay * 2.0).min(RECONNECT_BACKOFF_MAX);
        }};
    }
    while !stop.is_set() && Instant::now() < deadline {
        let mut p = payload.clone();
        let uuid = uuid::Uuid::new_v4().to_string();
        p.insert("stub_uuid".into(), json!(uuid));
        let (mut conn, registered) = match tokio::time::timeout(
            HANDSHAKE_TIMEOUT,
            handshake(socket, &p, limit),
        )
        .await
        {
            Ok(Ok(x)) => x,
            Ok(Err(HsErr::Stale(r))) => {
                log!(
                    WARNING,
                    "stub reconnect: handshake refused the new gateway generation ({r}); terminal, not retried pool={label}"
                );
                return None;
            }
            Ok(Err(HsErr::Fallback(r))) => {
                log!(
                    INFO,
                    "stub reconnect: gateway not back yet ({r}) next_retry_in={delay:.1}s pool={label}"
                );
                pause!();
                continue;
            }
            Err(_) => {
                log!(
                    INFO,
                    "stub reconnect: gateway not back yet (timeout) next_retry_in={delay:.1}s pool={label}"
                );
                pause!();
                continue;
            }
        };
        let caps = caps_of(&registered);
        if must_degrade_unshareable(poolable, &caps) {
            close(&conn.writer).await;
            log!(
                WARNING,
                "stub reconnect: the new gateway generation does not honour the poolable field and this server is not shareable; refusing rather than risk being pooled pool={label}"
            );
            return None;
        }
        let server = payload
            .get("server_name")
            .and_then(Value::as_str)
            .unwrap_or("");
        if must_degrade_nonce_blind(server, poolable, &caps) {
            close(&conn.writer).await;
            log!(
                WARNING,
                "stub reconnect: the new gateway generation mints no per-connection tenant nonce; refusing pool={label}"
            );
            return None;
        }
        if has_cap(&caps, "ensure_backend") {
            let budget = deadline
                .saturating_duration_since(Instant::now())
                .max(Duration::from_secs(1));
            let (outcome, verdict) =
                ensure_backend_admitted(&mut conn, has_cap(&caps, "spawn_queue"), budget).await;
            if outcome != Admit::Ready {
                close(&conn.writer).await;
                let cls = verdict.as_ref().and_then(rejection_class);
                let fallback_tag = verdict
                    .as_ref()
                    .and_then(|v| v.get("fallback"))
                    .map(truthy)
                    .unwrap_or(false);
                let refuse = outcome == Admit::Rejected
                    && (matches!(cls.as_deref(), Some("compat" | "isolation"))
                        || (cls.is_none() && !fallback_tag));
                if refuse {
                    log!(
                        WARNING,
                        "stub reconnect: gateway rejected ensure_backend ({cls:?}); refusing pool={label}"
                    );
                    return None;
                }
                log!(
                    INFO,
                    "stub reconnect: not admitted yet ({outcome:?}); retrying pool={label}"
                );
                pause!();
                continue;
            }
        }
        let (status, forward, detail) = replay_initialize(&mut conn, meta).await;
        if status == Replay::Retry {
            close(&conn.writer).await;
            log!(
                INFO,
                "stub reconnect: lost the connection during handshake replay ({detail}); retrying pool={label}"
            );
            pause!();
            continue;
        }
        if status != Replay::Ok {
            close(&conn.writer).await;
            log!(
                WARNING,
                "stub reconnect: refusing the new gateway generation ({detail}) pool={label}"
            );
            return None;
        }
        if let Some(fwd) = forward {
            if !stdout().emit_bounded(fwd.clone(), ERROR_EMIT_TIMEOUT).await {
                close(&conn.writer).await;
                log!(
                    WARNING,
                    "stub reconnect: could not deliver the replayed handshake pool={label}"
                );
                return None;
            }
            if let Some(m) = parse_obj(&fwd) {
                meta.note_init_result(&m);
            }
        }
        meta.reconnects += 1;
        log!(
            INFO,
            "stub reconnected to a restarted gateway ({detail}, reconnect #{}) pool={label}",
            meta.reconnects
        );
        return Some(Attached {
            conn,
            registered,
            stub_uuid: uuid,
        });
    }
    None
}

pub fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().map(|f| f != 0.0).unwrap_or(true),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// ``_is_failable_request``.
fn failable_id(msg: Option<&Obj>) -> Option<Value> {
    let m = msg?;
    let method = m.get("method")?;
    let id = m.get("id")?;
    if method.as_str() == Some("initialize") {
        return None;
    }
    Some(id.clone())
}

/// ``_reconnect_while_draining``: reconnect, while kiro-cli's stream is still
/// read, held for the grace window and failed retryably past it.
#[allow(clippy::too_many_arguments)]
pub async fn reconnect_while_draining(
    socket: &str,
    payload: &Obj,
    meta: &mut Meta,
    input: &mut Input,
    stop: &Stop,
    poolable: bool,
    label: &str,
    limits: Limits,
) -> Option<Attached> {
    struct Held {
        line: Vec<u8>,
        fail_id: Option<Value>,
        deadline: Instant,
    }
    let grace = queued_grace();
    let mut held: Vec<Held> = Vec::new();
    let mut held_bytes = 0usize;
    let rc = reconnect(socket, payload, meta, stop, poolable, label, limits.read);
    tokio::pin!(rc);
    let mut eof = false;
    let result = loop {
        if !eof {
            let now = Instant::now();
            let expired: Vec<Value> = held
                .iter()
                .filter(|h| h.fail_id.as_ref().is_some_and(|v| !v.is_null()) && h.deadline <= now)
                .map(|h| h.fail_id.clone().unwrap())
                .collect();
            if !expired.is_empty() {
                held.retain(|h| {
                    !(h.fail_id.as_ref().is_some_and(|v| !v.is_null()) && h.deadline <= now)
                });
                held_bytes = held.iter().map(|h| h.line.len()).sum();
                emit_error_frames(&expired, QUEUED_FAIL_MESSAGE, label).await;
                log!(
                    INFO,
                    "stub reconnect: failed {} call(s) queued while disconnected pool={label}",
                    expired.len()
                );
            }
        }
        let next = held
            .iter()
            .filter(|h| h.fail_id.as_ref().is_some_and(|v| !v.is_null()))
            .map(|h| h.deadline)
            .min();
        tokio::select! {
            biased;
            r = &mut rc => break r,
            line = input.next_line(), if !eof => {
                if line.is_empty() {
                    held.push(Held { line, fail_id: None, deadline: Instant::now() });
                    eof = true;
                    continue;
                }
                let msg = parse_obj(&line);
                let fid = failable_id(msg.as_ref());
                let no_room = held.len() >= crate::io::STDIN_QUEUE_MAXSIZE
                    || line.len() > limits.held_frame
                    || held_bytes + line.len() > limits.held_total;
                if no_room {
                    if let Some(id) = fid {
                        emit_error_frames(&[id], QUEUED_FAIL_MESSAGE, label).await;
                    } else {
                        log!(WARNING, "stub reconnect: dropped an unanswerable frame at the retention bound, held={}/{} bytes={}/{} pool={label}",
                             held.len(), crate::io::STDIN_QUEUE_MAXSIZE, held_bytes, limits.held_total);
                    }
                    continue;
                }
                held_bytes += line.len();
                let deadline = Instant::now() + grace;
                held.push(Held { line, fail_id: fid, deadline });
            }
            _ = sleep_until_opt(next), if next.is_some() && !eof => {}
        }
    };
    input.push_front(held.into_iter().map(|h| h.line).collect());
    result
}

async fn sleep_until_opt(t: Option<Instant>) {
    match t {
        Some(t) => tokio::time::sleep_until(t.into()).await,
        None => std::future::pending().await,
    }
}

/// ``_split_abandoned_ids``.
pub fn split_abandoned_ids(meta: &Meta) -> (Vec<Value>, Option<Value>) {
    let mut deferred = None;
    if meta.captured_init.is_some()
        && meta.init_result.is_none()
        && let Some(id) = meta.captured_init_id()
        && !id.is_null()
        && meta.outstanding.contains(&id)
    {
        deferred = Some(id);
    }
    let rest = meta
        .outstanding
        .iter()
        .filter(|i| Some(*i) != deferred.as_ref())
        .cloned()
        .collect();
    (rest, deferred)
}

/// ``_serve_capacity_refusal``.
pub async fn serve_capacity_refusal(
    input: &mut Input,
    stop: &Stop,
    reason: &str,
    retry_after: Option<i64>,
    label: &str,
) -> i32 {
    let message = format!("MCP gateway at capacity for {label}: {reason}");
    while !stop.is_set() {
        let line = tokio::select! {
            l = input.next_line() => l,
            _ = stop.wait() => return 0,
        };
        if line.is_empty() {
            return 0;
        }
        let Some(msg) = parse_obj(&line) else {
            continue;
        };
        let (Some(_), Some(id)) = (msg.get("method"), msg.get("id")) else {
            continue;
        };
        let mut f = Obj::new();
        f.insert("jsonrpc".into(), json!("2.0"));
        f.insert("id".into(), id.clone());
        f.insert(
            "error".into(),
            json!({"code": CAPACITY_ERROR_CODE, "message": message,
                   "data": {"class": "capacity", "retry_after_secs": retry_after}}),
        );
        if !stdout()
            .emit_bounded(frame_bytes(&f), ERROR_EMIT_TIMEOUT)
            .await
        {
            return 1;
        }
    }
    0
}

/// Request ids of every line nobody will forward (terminal path).
pub fn take_pending_request_ids(input: &mut Input) -> Vec<Value> {
    input
        .take_pending()
        .iter()
        .filter_map(|l| {
            let m = parse_obj(l)?;
            m.get("method")?;
            m.get("id").cloned()
        })
        .collect()
}
