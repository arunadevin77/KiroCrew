//! kirocrew-mcp-stub-rs: Rust port of ``kiro_crew.mcp_gateway.stub``.
//!
//! kiro-cli execs this in place of an MCP server. It registers with gatewayd
//! over the unix socket and bridges kiro-cli's stdio to it; when the gateway
//! cannot serve, it records why and execs the real backend in place. The flow
//! below is ``stub._amain`` step for step.

mod args;
mod bridge;
mod fallback;
mod handoff;
mod io;
mod payload;
mod util;

use bridge::{
    Admit, HsErr, Limits, Meta, RECONNECTABLE, Stop, caps_of, close, emit_error_frames,
    ensure_backend_admitted, handshake, has_cap, must_degrade_nonce_blind,
    must_degrade_unknown_target, must_degrade_unshareable, reconnect_while_draining,
    rejection_class, run_bridge, serve_capacity_refusal, split_abandoned_ids,
    take_pending_request_ids, truthy,
};
use fallback::{fallback_exec, keep_fd_across_exec, log_fallback};
use serde_json::Value;
use std::time::Duration;
use util::{ERROR, INFO, WARNING};

/// The crate-source digest ``build.rs`` computed, in a form
/// ``stub_impl._binary_source_digest`` finds by scanning the file: a stale
/// build is then never launched as the current stub. ``#[used]`` keeps it
/// through LTO and stripping, which drop symbols but not data.
#[used]
static SOURCE_DIGEST_MARKER: [u8; 89] = *concat!(
    "kirocrew-stub-rs-source:",
    env!("KIROCREW_STUB_RS_SOURCE_DIGEST"),
    ";"
)
.as_bytes()
.first_chunk::<89>()
.expect("24 + 64 + 1 bytes");

fn main() {
    util::init_from_env();
    let raw: Vec<String> = std::env::args().skip(1).collect();
    let a = args::parse(&raw);

    if std::env::var_os("KIROCREW_STUB_RS_DUMP_REGISTER").is_some() {
        // Conformance seam: print the Register payload and exit, so a test can
        // compare it field by field with ``stub.build_register_payload``.
        let p = payload::build_register_payload(&a);
        println!("{}", Value::Object(p));
        return;
    }

    if payload::verified_token_session_key().is_empty() {
        // Identity beyond the signed token stays with the Python ladder.
        handoff::run_python_stub(&a, "no verified session token");
    }

    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_io()
        .enable_time()
        .build()
        .expect("tokio runtime");
    let rc = rt.block_on(amain(a));
    io::stdout().drain_before_exit(Duration::from_secs(2));
    std::process::exit(rc);
}

fn reason_str(v: Option<&Value>, default: &str) -> String {
    match v {
        Some(Value::String(s)) if !s.is_empty() => s.clone(),
        Some(Value::Null) | None => default.to_string(),
        Some(Value::String(_)) => default.to_string(),
        Some(other) => other.to_string(),
    }
}

async fn amain(a: args::Args) -> i32 {
    let limits = Limits::adopt(a.read_limit.as_deref());
    let mut payload = payload::build_register_payload(&a);
    let label = payload::pool_label(&payload);
    let first_uuid = payload["stub_uuid"].as_str().unwrap_or("").to_string();

    let (stop_tx, stop_rx) = tokio::sync::watch::channel(false);
    let stop = Stop(stop_rx);
    tokio::spawn(async move {
        use tokio::signal::unix::{SignalKind, signal};
        let (Ok(mut t), Ok(mut i)) = (
            signal(SignalKind::terminate()),
            signal(SignalKind::interrupt()),
        ) else {
            return;
        };
        tokio::select! { _ = t.recv() => {}, _ = i.recv() => {} }
        let _ = stop_tx.send(true);
        std::future::pending::<()>().await;
    });

    let hs = tokio::time::timeout(
        bridge::HANDSHAKE_TIMEOUT,
        handshake(&a.socket, &payload, limits.read),
    )
    .await;
    let (mut conn, registered) = match hs {
        Err(_) => {
            log_fallback("handshake_timeout", &first_uuid, &label, &a, false);
            log!(WARNING, "handshake timed out; falling back pool={label}");
            fallback_exec(&a);
        }
        Ok(Err(HsErr::Fallback(r) | HsErr::Stale(r))) => {
            log_fallback(&r, &first_uuid, &label, &a, false);
            log!(WARNING, "handshake failed ({r}); falling back pool={label}");
            fallback_exec(&a);
        }
        Ok(Ok(x)) => x,
    };
    let mut stub_uuid = first_uuid.clone();
    log!(INFO, "registered stub_uuid={stub_uuid} pool={label}");
    let caps = caps_of(&registered);

    if must_degrade_unshareable(a.poolable, &caps) {
        log_fallback(
            "gateway does not honour the poolable field",
            &first_uuid,
            &label,
            &a,
            false,
        );
        log!(
            WARNING,
            "handshake: gateway did not advertise poolable_ack and this server is not shareable; falling back to a per-session exec rather than risk being pooled pool={label}"
        );
        close(&conn.writer).await;
        fallback_exec(&a);
    }
    if must_degrade_nonce_blind(&a.server, a.poolable, &caps) {
        log_fallback(
            "gateway mints no per-connection tenant nonce",
            &first_uuid,
            &label,
            &a,
            false,
        );
        log!(
            WARNING,
            "handshake: gateway did not advertise tenant_nonce; falling back pool={label}"
        );
        close(&conn.writer).await;
        fallback_exec(&a);
    }

    let mut input = io::Input::new();

    if has_cap(&caps, "ensure_backend") {
        let queue_aware = has_cap(&caps, "spawn_queue");
        let (outcome, ready) =
            ensure_backend_admitted(&mut conn, queue_aware, bridge::reconnect_budget()).await;
        match outcome {
            Admit::Timeout | Admit::Closed => {
                close(&conn.writer).await;
                let o = if outcome == Admit::Timeout {
                    "timeout"
                } else {
                    "closed"
                };
                log_fallback(
                    &format!("ensure_backend_io:{o}"),
                    &first_uuid,
                    &label,
                    &a,
                    false,
                );
                log!(WARNING, "ensure_backend {o}; falling back pool={label}");
                fallback_exec(&a);
            }
            Admit::Rejected => {
                let ready = ready.unwrap_or_default();
                let cls = rejection_class(&ready);
                let reason = reason_str(ready.get("reason"), "ensure_backend_rejected");
                if cls.as_deref() == Some("capacity") {
                    let retry_after = ready
                        .get("retry_after_secs")
                        .and_then(|v| if v.is_boolean() { None } else { v.as_i64() });
                    close(&conn.writer).await;
                    log_fallback(&format!("capacity:{reason}"), &first_uuid, &label, &a, true);
                    log!(
                        WARNING,
                        "gateway refused ensure_backend for capacity ({reason}, retry_after={retry_after:?}); answering requests with a typed error pool={label}"
                    );
                    return serve_capacity_refusal(&mut input, &stop, &reason, retry_after, &label)
                        .await;
                }
                let eligible = match cls.as_deref() {
                    Some(c) => c == "compat" || c == "isolation",
                    None => {
                        ready.get("fallback").map(truthy).unwrap_or(false)
                            || must_degrade_unknown_target(&reason)
                    }
                };
                if !eligible {
                    close(&conn.writer).await;
                    log_fallback(&format!("terminal:{reason}"), &first_uuid, &label, &a, true);
                    log!(
                        ERROR,
                        "gateway terminally rejected ensure_backend ({reason}); not falling back pool={label}"
                    );
                    return 1;
                }
                if queue_aware {
                    keep_fd_across_exec(conn.fd);
                } else {
                    close(&conn.writer).await;
                }
                log_fallback(&reason, &first_uuid, &label, &a, false);
                log!(
                    WARNING,
                    "gateway fallback-rejected ensure_backend ({reason}, class={}); falling back pool={label}",
                    cls.as_deref().unwrap_or("legacy")
                );
                fallback_exec(&a);
            }
            Admit::Ready => {}
        }
    }

    let mut meta = Meta::default();
    let mut peer_ping = has_cap(&caps, "bridge_ping");
    loop {
        let recaller = if payload
            .get("session_key")
            .and_then(Value::as_str)
            .unwrap_or("")
            .is_empty()
        {
            Some(payload::resolve_channel_id(a.channel_id.as_deref()))
        } else {
            None
        };
        run_bridge(conn, &stop, &mut input, &mut meta, peer_ping, recaller).await;

        if !RECONNECTABLE.contains(&meta.reason.as_str()) {
            break;
        }
        let (to_fail, deferred) = split_abandoned_ids(&meta);
        emit_error_frames(
            &to_fail,
            "Gateway restarted; this call was failed so it would not hang. Retry it.",
            &label,
        )
        .await;
        meta.outstanding = deferred.into_iter().collect();

        if meta.has_live_subscriptions() {
            log!(
                WARNING,
                "stub bridge ended ({}) with live resource subscriptions; refusing to reconnect because they cannot be re-established here pool={label}",
                meta.reason
            );
            break;
        }
        if meta.captured_init.is_none() {
            log!(
                WARNING,
                "stub bridge ended ({}) before initialize was seen; no reconnect is possible pool={label}",
                meta.reason
            );
            break;
        }
        payload = payload::build_register_payload(&a);
        log_fallback(
            &format!("bridge_lost_{}", meta.reason),
            &stub_uuid,
            &label,
            &a,
            false,
        );
        let attached = reconnect_while_draining(
            &a.socket, &payload, &mut meta, &mut input, &stop, a.poolable, &label, limits,
        )
        .await;
        let Some(att) = attached else {
            log!(
                WARNING,
                "stub could not re-attach to the gateway after {}; this session loses these servers pool={label}",
                meta.reason
            );
            break;
        };
        conn = att.conn;
        stub_uuid = att.stub_uuid;
        peer_ping = has_cap(&caps_of(&att.registered), "bridge_ping");
    }

    for id in take_pending_request_ids(&mut input) {
        if !meta.outstanding.contains(&id) {
            meta.outstanding.push(id);
        }
    }
    if !meta.outstanding.is_empty() {
        let abandoned = std::mem::take(&mut meta.outstanding);
        emit_error_frames(
            &abandoned,
            "Gateway stopped responding and could not be reached again; this call was failed so it would not hang. Start a new session to restore these tools.",
            &label,
        )
        .await;
        log_fallback(
            &format!("bridge_dead_{}", meta.reason),
            &stub_uuid,
            &label,
            &a,
            false,
        );
        log!(
            WARNING,
            "bridge ended ({}) with {} outstanding call(s) after {} reconnect(s) pool={label}",
            meta.reason,
            abandoned.len(),
            meta.reconnects
        );
        return 1;
    }
    if RECONNECTABLE.contains(&meta.reason.as_str()) {
        log_fallback(
            &format!("bridge_dead_{}", meta.reason),
            &stub_uuid,
            &label,
            &a,
            false,
        );
        log!(
            WARNING,
            "bridge ended ({}) and could not re-attach after {} reconnect(s); this session loses these servers pool={label}",
            meta.reason,
            meta.reconnects
        );
        return 1;
    }
    0
}
