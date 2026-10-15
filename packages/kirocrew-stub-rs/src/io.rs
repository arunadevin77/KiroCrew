//! Stdio and socket plumbing.
//!
//! stdin and stdout each get ONE dedicated OS thread for the life of the
//! process, mirroring the Python stub's ``stub-stdin`` / ``stub-stdout`` daemon
//! threads: a blocked read or a stalled kiro-cli reader can never wedge the
//! event loop or a graceful exit. Both hand off through bounded queues so a
//! slow side applies backpressure instead of growing RSS.

use serde_json::{Map, Value};
use std::collections::VecDeque;
use std::io::{BufRead, Write};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::mpsc::{SyncSender, TrySendError, sync_channel};
use std::sync::{Arc, OnceLock};
use std::time::{Duration, Instant};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::unix::{OwnedReadHalf, OwnedWriteHalf};
use tokio::sync::{Mutex, Notify, mpsc};

pub const STDIN_QUEUE_MAXSIZE: usize = 256;

// ---------------------------------------------------------------------------
// JSON line parsing (json_line.parse_json_object_line).
// ---------------------------------------------------------------------------

pub type Obj = Map<String, Value>;

fn parse_text(text: &str) -> Option<Obj> {
    match serde_json::from_str::<Value>(text) {
        Ok(Value::Object(m)) => Some(m),
        Ok(_) => None,
        Err(_) => {
            let stripped = crate::util::py_strip(text);
            if stripped.len() == text.trim_matches(|c| " \t\n\r".contains(c)).len() {
                return None;
            }
            match serde_json::from_str::<Value>(stripped) {
                Ok(Value::Object(m)) => Some(m),
                _ => None,
            }
        }
    }
}

/// ``errors="strict"``: bytes that are not UTF-8 cost the line.
pub fn parse_obj(line: &[u8]) -> Option<Obj> {
    parse_text(std::str::from_utf8(line).ok()?)
}

/// ``errors="replace"``.
pub fn parse_obj_lossy(line: &[u8]) -> Option<Obj> {
    parse_text(&String::from_utf8_lossy(line))
}

/// ``_is_json_non_object``: valid JSON that is neither an object nor null.
pub fn is_json_non_object(line: &[u8]) -> bool {
    matches!(serde_json::from_slice::<Value>(String::from_utf8_lossy(line).as_bytes()),
             Ok(v) if !v.is_null() && !v.is_object())
}

/// Hashable request-id key, matching what a Python ``set`` would accept.
pub fn id_key(id: &Value) -> Option<String> {
    match id {
        Value::Array(_) | Value::Object(_) => None,
        Value::Number(n) => {
            // 1 and 1.0 are one key in a Python set.
            if let Some(f) = n.as_f64()
                && f.fract() == 0.0
                && f.abs() < 9.0e15
            {
                return Some(format!("n:{}", f as i64));
            }
            Some(format!("n:{n}"))
        }
        Value::Bool(b) => Some(format!("n:{}", *b as i64)),
        other => Some(other.to_string()),
    }
}

pub fn frame_bytes(obj: &Obj) -> Vec<u8> {
    let mut v = serde_json::to_vec(obj).unwrap_or_default();
    v.push(b'\n');
    v
}

// ---------------------------------------------------------------------------
// stdin: one reader thread, started lazily, plus a head buffer.
// ---------------------------------------------------------------------------

/// The inbound stream as kiro-cli sent it. Not started until first read: a
/// fallback exec must find ``initialize`` still unread on fd 0.
pub struct Input {
    rx: Option<mpsc::Receiver<Vec<u8>>>,
    head: VecDeque<Vec<u8>>,
}

impl Input {
    pub fn new() -> Self {
        Self {
            rx: None,
            head: VecDeque::new(),
        }
    }

    fn start(&mut self) -> &mut mpsc::Receiver<Vec<u8>> {
        if self.rx.is_none() {
            let (tx, rx) = mpsc::channel::<Vec<u8>>(STDIN_QUEUE_MAXSIZE);
            std::thread::Builder::new()
                .name("stub-stdin".into())
                .stack_size(64 * 1024)
                .spawn(move || {
                    let stdin = std::io::stdin();
                    let mut r = std::io::BufReader::with_capacity(64 * 1024, stdin.lock());
                    loop {
                        let mut line = Vec::new();
                        let n = r.read_until(b'\n', &mut line).unwrap_or(0);
                        let eof = n == 0;
                        if tx
                            .blocking_send(if eof { Vec::new() } else { line })
                            .is_err()
                            || eof
                        {
                            return;
                        }
                    }
                })
                .expect("spawn stdin reader");
            self.rx = Some(rx);
        }
        self.rx.as_mut().unwrap()
    }

    /// One line; empty means EOF. Cancel-safe.
    pub async fn next_line(&mut self) -> Vec<u8> {
        if let Some(l) = self.head.pop_front() {
            return l;
        }
        self.start().recv().await.unwrap_or_default()
    }

    pub fn push_front(&mut self, lines: Vec<Vec<u8>>) {
        for l in lines.into_iter().rev() {
            self.head.push_front(l);
        }
    }

    /// Every line waiting anywhere (head, then the queue), removed.
    pub fn take_pending(&mut self) -> Vec<Vec<u8>> {
        let mut out: Vec<Vec<u8>> = self.head.drain(..).collect();
        if let Some(rx) = self.rx.as_mut() {
            while let Ok(l) = rx.try_recv() {
                out.push(l);
            }
        }
        out
    }
}

// ---------------------------------------------------------------------------
// stdout: one writer thread for the life of the process.
// ---------------------------------------------------------------------------

pub struct Stdout {
    tx: SyncSender<Vec<u8>>,
    pub failed: Arc<AtomicBool>,
    pub failed_notify: Arc<Notify>,
    pending: Arc<AtomicUsize>,
}

static STDOUT: OnceLock<Stdout> = OnceLock::new();

pub fn stdout() -> &'static Stdout {
    STDOUT.get_or_init(|| {
        let (tx, rx) = sync_channel::<Vec<u8>>(256);
        let failed = Arc::new(AtomicBool::new(false));
        let notify = Arc::new(Notify::new());
        let pending = Arc::new(AtomicUsize::new(0));
        let (f2, n2, p2) = (failed.clone(), notify.clone(), pending.clone());
        std::thread::Builder::new()
            .name("stub-stdout".into())
            .stack_size(64 * 1024)
            .spawn(move || {
                let out = std::io::stdout();
                let mut lock = out.lock();
                while let Ok(item) = rx.recv() {
                    let ok = lock.write_all(&item).and_then(|_| lock.flush()).is_ok();
                    p2.fetch_sub(1, Ordering::AcqRel);
                    if !ok {
                        f2.store(true, Ordering::Release);
                        n2.notify_one();
                        return;
                    }
                }
            })
            .expect("spawn stdout writer");
        Stdout {
            tx,
            failed,
            failed_notify: notify,
            pending,
        }
    })
}

#[derive(Debug)]
pub struct WriterDead;

impl Stdout {
    /// Bounded put with cancellable 50 ms backpressure, as ``_emit``.
    pub async fn emit(&self, line: Vec<u8>) -> Result<(), WriterDead> {
        let mut item = line;
        loop {
            if self.failed.load(Ordering::Acquire) {
                return Err(WriterDead);
            }
            self.pending.fetch_add(1, Ordering::AcqRel);
            match self.tx.try_send(item) {
                Ok(()) => return Ok(()),
                Err(TrySendError::Full(back)) => {
                    self.pending.fetch_sub(1, Ordering::AcqRel);
                    item = back;
                    tokio::time::sleep(Duration::from_millis(50)).await;
                }
                Err(TrySendError::Disconnected(_)) => {
                    self.pending.fetch_sub(1, Ordering::AcqRel);
                    return Err(WriterDead);
                }
            }
        }
    }

    /// Emit with a hard deadline (``_ERROR_EMIT_TIMEOUT_SECS`` paths).
    pub async fn emit_bounded(&self, line: Vec<u8>, timeout: Duration) -> bool {
        matches!(
            tokio::time::timeout(timeout, self.emit(line)).await,
            Ok(Ok(()))
        )
    }

    /// Give queued frames a bounded chance to reach kiro-cli before exit.
    pub fn drain_before_exit(&self, budget: Duration) {
        let deadline = Instant::now() + budget;
        while self.pending.load(Ordering::Acquire) > 0
            && !self.failed.load(Ordering::Acquire)
            && Instant::now() < deadline
        {
            std::thread::sleep(Duration::from_millis(2));
        }
    }
}

// ---------------------------------------------------------------------------
// Gateway socket: newline frames under a read ceiling.
// ---------------------------------------------------------------------------

pub enum ReadErr {
    /// EOF, a short read, or a frame over the ceiling (asyncio's
    /// IncompleteReadError / LimitOverrunError): all read as "no frame".
    Closed,
}

pub struct SockReader {
    inner: BufReader<OwnedReadHalf>,
    limit: usize,
    partial: Vec<u8>,
}

impl SockReader {
    pub fn new(r: OwnedReadHalf, limit: usize) -> Self {
        Self {
            inner: BufReader::with_capacity(64 * 1024, r),
            limit,
            partial: Vec::new(),
        }
    }

    /// One whole ``\n``-terminated frame. Cancel-safe: a partial frame is kept
    /// in ``self`` across a dropped call.
    pub async fn read_line(&mut self) -> Result<Vec<u8>, ReadErr> {
        loop {
            let buf = match self.inner.fill_buf().await {
                Ok(b) => b,
                Err(_) => return Err(ReadErr::Closed),
            };
            if buf.is_empty() {
                return Err(ReadErr::Closed);
            }
            if let Some(pos) = buf.iter().position(|b| *b == b'\n') {
                if self.partial.len() + pos + 1 > self.limit {
                    return Err(ReadErr::Closed);
                }
                self.partial.extend_from_slice(&buf[..=pos]);
                self.inner.consume(pos + 1);
                return Ok(std::mem::take(&mut self.partial));
            }
            let n = buf.len();
            if self.partial.len() + n > self.limit {
                return Err(ReadErr::Closed);
            }
            self.partial.extend_from_slice(buf);
            self.inner.consume(n);
        }
    }
}

/// The one stub->gateway writer, serialized like ``_mc_write_lock``.
pub type SockWriter = std::rc::Rc<Mutex<OwnedWriteHalf>>;

pub async fn sock_write(w: &SockWriter, bytes: &[u8]) -> std::io::Result<()> {
    let mut g = w.lock().await;
    g.write_all(bytes).await?;
    g.flush().await
}

pub async fn write_frame(w: &SockWriter, obj: &Obj) -> std::io::Result<()> {
    sock_write(w, &frame_bytes(obj)).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_contract() {
        assert!(parse_obj(b"{\"a\":1}\n").is_some());
        assert!(parse_obj(b"null\n").is_none());
        assert!(parse_obj(b"[1]\n").is_none());
        assert!(parse_obj(b"\x1e{\"a\":1}\n").is_some());
        assert!(parse_obj(b"\xff{}\n").is_none());
        assert!(parse_obj_lossy(b"{\"a\":\"\xff\"}\n").is_some());
        assert!(is_json_non_object(b"[1]"));
        assert!(!is_json_non_object(b"null"));
    }

    #[test]
    fn id_keys_follow_python_set_equality() {
        use serde_json::json;
        assert_eq!(id_key(&json!(1)), id_key(&json!(1.0)));
        assert_ne!(id_key(&json!(1)), id_key(&json!("1")));
        assert!(id_key(&json!([1])).is_none());
    }
}
