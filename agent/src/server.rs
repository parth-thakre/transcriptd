//! WebSocket clients for dictation and live meetings.

use anyhow::{anyhow, Context, Result};
use futures_util::{SinkExt, StreamExt};
use tokio::sync::{mpsc, oneshot, Mutex};
use std::time::Duration;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::Message;

use crate::config::AgentConfig;

/// How long opening a connection to the server may take. The agent's command loop
/// waits on it, so an unreachable host must not hold it for the OS default (minutes).
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);

/// One rustls client config for `wss://` and `https://`: the ring provider and the
/// operating system's trust store (so a private CA added to the system works too).
pub fn tls_config() -> Result<std::sync::Arc<rustls::ClientConfig>> {
    static CONFIG: std::sync::OnceLock<std::result::Result<std::sync::Arc<rustls::ClientConfig>, String>> = std::sync::OnceLock::new();
    CONFIG
        .get_or_init(|| {
            let native = rustls_native_certs::load_native_certs();
            for e in &native.errors {
                tracing::warn!("loading system root certificates: {e}");
            }
            let mut roots = rustls::RootCertStore::empty();
            let (added, ignored) = roots.add_parsable_certificates(native.certs);
            if ignored > 0 {
                tracing::debug!("ignored {ignored} unparsable system root certificate(s)");
            }
            if added == 0 {
                return Err("no usable system root certificates found for TLS".into());
            }
            let config = rustls::ClientConfig::builder_with_provider(std::sync::Arc::new(rustls::crypto::ring::default_provider()))
                .with_safe_default_protocol_versions()
                .map_err(|e| e.to_string())?
                .with_root_certificates(roots)
                .with_no_client_auth();
            Ok(std::sync::Arc::new(config))
        })
        .clone()
        .map_err(|e| anyhow!(e))
}

type WsStream = tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

/// Open an authenticated WebSocket to `path` on the configured server (`ws://` for an
/// `http://` server URL, `wss://` for `https://`).
async fn connect_ws(cfg: &AgentConfig, path: &str) -> Result<WsStream> {
    let url = cfg.ws_url(path);
    let mut req = url.as_str().into_client_request().context("bad server url")?;
    req.headers_mut().insert("Authorization", format!("Bearer {}", cfg.token).parse()?);
    let connector = if url.starts_with("wss://") { Some(tokio_tungstenite::Connector::Rustls(tls_config()?)) } else { None };
    let (ws, _) = tokio::time::timeout(CONNECT_TIMEOUT, tokio_tungstenite::connect_async_tls_with_config(req, None, false, connector))
        .await
        .with_context(|| format!("connect {url}: timed out"))?
        .with_context(|| format!("connect {url}"))?;
    Ok(ws)
}

#[derive(Debug)]
pub enum ServerEvent {
    Ready,
    Partial(String),
    Final(serde_json::Value),
    MeetingSegment(serde_json::Value),
    Error(String),
    Closed,
}

pub struct DictateSession {
    audio_tx: mpsc::Sender<Vec<i16>>,
    ctl_tx: mpsc::Sender<&'static str>,
    pub events: mpsc::Receiver<ServerEvent>,
    /// Writer and reader tasks, aborted if a cancel cannot get through.
    tasks: [tokio::task::AbortHandle; 2],
}

/// How long a cancelled session may take to tell the server before its tasks are
/// aborted (a dead connection would otherwise hold them, and the audio pump, forever).
const CANCEL_GRACE: std::time::Duration = std::time::Duration::from_secs(2);

impl DictateSession {
    pub async fn open(cfg: &AgentConfig, app_hint: Option<String>, context: Option<serde_json::Value>) -> Result<Self> {
        let ws = connect_ws(cfg, "/v1/dictate").await?;
        let (mut sink, mut stream) = ws.split();
        let start = serde_json::json!({
            "type": "start",
            "profile": cfg.profile,
            "language": cfg.language,
            "app_hint": app_hint,
            "partials": cfg.partials,
            "context": context,
        });
        sink.send(Message::Text(start.to_string().into())).await?;

        let (audio_tx, mut audio_rx) = mpsc::channel::<Vec<i16>>(512);
        let (ctl_tx, mut ctl_rx) = mpsc::channel::<&'static str>(4);
        let (ev_tx, events) = mpsc::channel::<ServerEvent>(32);

        // Writer task.
        let writer = tokio::spawn(async move {
            fn binary(frame: Vec<i16>) -> Message {
                Message::Binary(frame.iter().flat_map(|s| s.to_le_bytes()).collect::<Vec<u8>>().into())
            }
            loop {
                tokio::select! {
                    Some(frame) = audio_rx.recv() => {
                        if sink.send(binary(frame)).await.is_err() { break; }
                    }
                    Some(cmd) = ctl_rx.recv() => {
                        // "end" comes after the last frame was queued; select! picks
                        // branches at random, so send what is still queued first.
                        if cmd == "end" {
                            let mut failed = false;
                            while let Ok(frame) = audio_rx.try_recv() {
                                if sink.send(binary(frame)).await.is_err() { failed = true; break; }
                            }
                            if failed { break; }
                        }
                        let msg = serde_json::json!({"type": cmd}).to_string();
                        let _ = sink.send(Message::Text(msg.into())).await;
                        if cmd == "cancel" { let _ = sink.close().await; break; }
                    }
                    else => break,
                }
            }
        });
        // Reader task.
        let reader = tokio::spawn(async move {
            while let Some(msg) = stream.next().await {
                let Ok(msg) = msg else { break };
                if let Message::Text(t) = msg {
                    let Ok(v) = serde_json::from_str::<serde_json::Value>(&t) else { continue };
                    let ev = match v.get("type").and_then(|s| s.as_str()) {
                        Some("ready") => ServerEvent::Ready,
                        Some("partial") => ServerEvent::Partial(v.get("text").and_then(|s| s.as_str()).unwrap_or("").to_string()),
                        Some("final") => ServerEvent::Final(v),
                        Some("error") => ServerEvent::Error(v.get("message").and_then(|s| s.as_str()).unwrap_or("error").to_string()),
                        Some("cancelled") => ServerEvent::Closed,
                        _ => continue,
                    };
                    let done = matches!(ev, ServerEvent::Final(_) | ServerEvent::Closed);
                    if ev_tx.send(ev).await.is_err() || done {
                        break;
                    }
                }
            }
            let _ = ev_tx.send(ServerEvent::Closed).await;
        });
        Ok(Self { audio_tx, ctl_tx, events, tasks: [writer.abort_handle(), reader.abort_handle()] })
    }

    pub fn audio_tx_clone(&self) -> mpsc::Sender<Vec<i16>> {
        self.audio_tx.clone()
    }

    #[allow(dead_code)]
    pub fn send_audio(&self, frame: Vec<i16>) -> Result<()> {
        self.audio_tx.try_send(frame).map_err(|_| anyhow!("audio channel full or closed"))
    }

    /// Send "end" once `flushed` completes (all captured audio queued), without making
    /// the caller wait for it: a stalled connection must not hold up the agent.
    pub fn end_after(&self, flushed: impl std::future::Future<Output = ()> + Send + 'static) {
        let ctl = self.ctl_tx.clone();
        tokio::spawn(async move {
            flushed.await;
            let _ = ctl.send("end").await;
        });
    }

    /// Tell the server to drop the take. Never waits on the connection; if the writer
    /// is stuck, both tasks are aborted after a grace period, which also releases a
    /// pump blocked on the full audio queue.
    pub async fn cancel(&self) {
        let _ = self.ctl_tx.try_send("cancel");
        let tasks = self.tasks.clone();
        tokio::spawn(async move {
            tokio::time::sleep(CANCEL_GRACE).await;
            for t in tasks {
                t.abort();
            }
        });
    }
}

/// A live meeting stream. Audio is queued without blocking the capture thread.
#[derive(Clone)]
pub struct MeetingSession {
    audio_tx: mpsc::Sender<Vec<i16>>,
    ctl_tx: mpsc::Sender<()>,
    ended: std::sync::Arc<Mutex<oneshot::Receiver<()>>>,
    pub events: std::sync::Arc<Mutex<mpsc::Receiver<ServerEvent>>>,
}

impl MeetingSession {
    pub async fn open(cfg: &AgentConfig, meeting_id: i64) -> Result<Self> {
        let ws = connect_ws(cfg, &format!("/v1/meetings/{meeting_id}/stream")).await?;
        let (mut sink, mut stream) = ws.split();
        let (audio_tx, mut audio_rx) = mpsc::channel::<Vec<i16>>(512);
        let (ctl_tx, mut ctl_rx) = mpsc::channel::<()>(1);
        let (ev_tx, events) = mpsc::channel::<ServerEvent>(32);
        let (ended_tx, ended) = oneshot::channel();

        tokio::spawn(async move {
            loop {
                tokio::select! {
                    biased;
                    Some(()) = ctl_rx.recv() => {
                        // Keep the final capture frames ahead of the end marker.
                        while let Ok(frame) = audio_rx.try_recv() {
                            if send_meeting_frame(&mut sink, frame).await.is_err() { return; }
                        }
                        let _ = sink.send(Message::Text(r#"{"type":"end"}"#.into())).await;
                        break;
                    }
                    Some(frame) = audio_rx.recv() => {
                        if send_meeting_frame(&mut sink, frame).await.is_err() { break; }
                    }
                    else => break,
                }
            }
        });
        tokio::spawn(async move {
            let mut ended_tx = Some(ended_tx);
            while let Some(msg) = stream.next().await {
                match msg {
                    Ok(Message::Text(t)) => {
                        let Ok(v) = serde_json::from_str::<serde_json::Value>(&t) else { continue };
                        match v.get("type").and_then(|s| s.as_str()) {
                            Some("segment") => {
                                if ev_tx.send(ServerEvent::MeetingSegment(v)).await.is_err() { break; }
                            }
                            Some("ended") => {
                                if let Some(tx) = ended_tx.take() { let _ = tx.send(()); }
                                break;
                            }
                            Some("error") => {
                                let message = v.get("message").and_then(|s| s.as_str()).unwrap_or("meeting stream error").to_string();
                                let _ = ev_tx.send(ServerEvent::Error(message)).await;
                                break;
                            }
                            _ => {}
                        }
                    }
                    Ok(Message::Close(_)) => break,
                    Err(e) => {
                        let _ = ev_tx.send(ServerEvent::Error(format!("meeting stream: {e}"))).await;
                        break;
                    }
                    _ => {}
                }
            }
        });
        Ok(Self { audio_tx, ctl_tx, ended: std::sync::Arc::new(Mutex::new(ended)), events: std::sync::Arc::new(Mutex::new(events)) })
    }

    pub fn send_frame(&self, interleaved: Vec<i16>) {
        // Losing a frame is preferable to holding up the WAV backup/capture thread.
        let _ = self.audio_tx.try_send(interleaved);
    }

    pub async fn end(&self) -> Result<()> {
        tokio::time::timeout(Duration::from_secs(30), async {
            self.ctl_tx.send(()).await.context("meeting stream writer stopped")?;
            (&mut *self.ended.lock().await).await.context("meeting stream closed before ended")
        })
        .await
        .context("timed out waiting for meeting ended")?
    }
}

async fn send_meeting_frame<S>(sink: &mut S, frame: Vec<i16>) -> Result<()>
where
    S: futures_util::Sink<Message, Error = tokio_tungstenite::tungstenite::Error> + Unpin,
{
    let bytes: Vec<u8> = frame.iter().flat_map(|s| s.to_le_bytes()).collect();
    sink.send(Message::Binary(bytes.into())).await?;
    Ok(())
}

pub async fn post_file(cfg: &AgentConfig, path: &str, file: &std::path::Path) -> Result<()> {
    reqwest_lite::Client::new().post_file(&cfg.http_url(path), &cfg.token, file).await
}

/// POST JSON helper for creating meetings.
pub async fn post_json(cfg: &AgentConfig, path: &str, body: serde_json::Value) -> Result<serde_json::Value> {
    reqwest_lite::Client::new().post_json(&cfg.http_url(path), &cfg.token, body).await
}

/// Minimal HTTP/1.1 client on top of tokio, over plain TCP (`http://`) or rustls (`https://`).
mod reqwest_lite {
    use anyhow::{anyhow, Context, Result};
    use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};

    pub trait Io: AsyncRead + AsyncWrite + Unpin + Send {}
    impl<T: AsyncRead + AsyncWrite + Unpin + Send> Io for T {}

    pub struct Client;
    impl Client {
        pub fn new() -> Self { Self }

        pub async fn post_json(&self, url: &str, token: &str, body: serde_json::Value) -> Result<serde_json::Value> {
            let payload = body.to_string();
            let (mut s, target) = connect(url).await?;
            let req = format!(
                "POST {} HTTP/1.1\r\nHost: {}\r\nAuthorization: Bearer {token}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{payload}",
                target.path, target.host_header, payload.len()
            );
            s.write_all(req.as_bytes()).await?;
            s.flush().await?;
            let body = response(&mut s).await?;
            Ok(serde_json::from_str(&body)?)
        }

        pub async fn post_file(&self, url: &str, token: &str, file: &std::path::Path) -> Result<()> {
            let boundary = format!("transcriptd-{}-{}", std::process::id(),
                std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_nanos());
            let prefix = format!("--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"meeting.wav\"\r\nContent-Type: audio/wav\r\n\r\n");
            let suffix = format!("\r\n--{boundary}--\r\n");
            let mut source = tokio::fs::File::open(file).await?;
            let length = prefix.len() as u64 + source.metadata().await?.len() + suffix.len() as u64;
            let (mut s, target) = connect(url).await?;
            let req = format!(
                "POST {} HTTP/1.1\r\nHost: {}\r\nAuthorization: Bearer {token}\r\nContent-Type: multipart/form-data; boundary={boundary}\r\nContent-Length: {length}\r\nConnection: close\r\n\r\n",
                target.path, target.host_header
            );
            s.write_all(req.as_bytes()).await?;
            s.write_all(prefix.as_bytes()).await?;
            tokio::io::copy(&mut source, &mut s).await?;
            s.write_all(suffix.as_bytes()).await?;
            s.flush().await?;
            response(&mut s).await?;
            Ok(())
        }
    }

    pub struct Target {
        pub path: String,
        pub host_header: String,
    }

    async fn connect(url: &str) -> Result<(Box<dyn Io>, Target)> {
        let u = url::Url::parse(url)?;
        let secure = match u.scheme() {
            "http" => false,
            "https" => true,
            other => return Err(anyhow!("unsupported server URL scheme {other:?} (use http:// or https://)")),
        };
        let host = u.host_str().ok_or_else(|| anyhow!("no host"))?;
        // host_str() keeps the brackets of an IPv6 literal; sockets and SNI want it bare.
        let bare = host.trim_start_matches('[').trim_end_matches(']').to_string();
        let port = u.port_or_known_default().unwrap_or(if secure { 443 } else { 80 });
        let path = match u.query() {
            Some(q) => format!("{}?{q}", u.path()),
            None => u.path().to_string(),
        };
        let host_header = match u.port() {
            Some(p) => format!("{host}:{p}"),
            None => host.to_string(),
        };
        let tcp = tokio::time::timeout(super::CONNECT_TIMEOUT, tokio::net::TcpStream::connect((bare.as_str(), port)))
            .await
            .with_context(|| format!("connect {host}:{port}: timed out"))??;
        let stream: Box<dyn Io> = if secure {
            let name = rustls::pki_types::ServerName::try_from(bare).context("invalid TLS server name")?;
            let tls = tokio_rustls::TlsConnector::from(super::tls_config()?)
                .connect(name, tcp)
                .await
                .with_context(|| format!("TLS handshake with {host}"))?;
            Box::new(tls)
        } else {
            Box::new(tcp)
        };
        Ok((stream, Target { path, host_header }))
    }

    async fn response(s: &mut Box<dyn Io>) -> Result<String> {
        let mut buf = Vec::new();
        let mut chunk = [0u8; 16 * 1024];
        loop {
            match s.read(&mut chunk).await {
                Ok(0) => break,
                Ok(n) => buf.extend_from_slice(&chunk[..n]),
                // Many TLS servers close a `Connection: close` response without close_notify.
                Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof && !buf.is_empty() => break,
                Err(e) => return Err(e.into()),
            }
        }
        parse_response(&buf)
    }

    /// Status check and body of a complete HTTP/1.1 response (`Content-Length`, read to
    /// close, or `Transfer-Encoding: chunked` as a reverse proxy may send).
    pub fn parse_response(raw: &[u8]) -> Result<String> {
        let split = raw.windows(4).position(|w| w == b"\r\n\r\n").ok_or_else(|| anyhow!("bad http response"))?;
        let head = String::from_utf8_lossy(&raw[..split]);
        let mut body = raw[split + 4..].to_vec();
        let status: u16 = head.split_whitespace().nth(1).and_then(|s| s.parse().ok()).unwrap_or(0);
        let chunked = head.lines().skip(1).any(|l| {
            l.split_once(':').is_some_and(|(k, v)| k.trim().eq_ignore_ascii_case("transfer-encoding") && v.to_ascii_lowercase().contains("chunked"))
        });
        if chunked {
            body = dechunk(&body)?;
        }
        let body = String::from_utf8_lossy(&body).into_owned();
        if !(200..300).contains(&status) {
            return Err(anyhow!("http {status}: {body}"));
        }
        Ok(body)
    }

    fn dechunk(mut data: &[u8]) -> Result<Vec<u8>> {
        let mut out = Vec::new();
        loop {
            let eol = data.windows(2).position(|w| w == b"\r\n").ok_or_else(|| anyhow!("bad chunked body"))?;
            let size_line = std::str::from_utf8(&data[..eol])?;
            let size = usize::from_str_radix(size_line.split(';').next().unwrap_or("").trim(), 16).context("bad chunk size")?;
            data = &data[eol + 2..];
            if size == 0 {
                return Ok(out);
            }
            anyhow::ensure!(data.len() >= size + 2, "truncated chunked body");
            out.extend_from_slice(&data[..size]);
            data = &data[size + 2..];
        }
    }

    #[cfg(test)]
    mod tests {
        use super::parse_response;

        #[test]
        fn plain_and_chunked_bodies() {
            let plain = b"HTTP/1.1 201 Created\r\nContent-Length: 8\r\n\r\n{\"id\":7}";
            assert_eq!(parse_response(plain).unwrap(), r#"{"id":7}"#);
            let chunked = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\n{\"id\r\n4;x=y\r\n\":7}\r\n0\r\n\r\n";
            assert_eq!(parse_response(chunked).unwrap(), r#"{"id":7}"#);
            let err = parse_response(b"HTTP/1.1 401 Unauthorized\r\n\r\nnope").unwrap_err();
            assert!(err.to_string().contains("401"));
            assert!(parse_response(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n9\r\nab").is_err());
        }
    }
}

#[cfg(test)]
mod dictate_tests {
    use super::*;
    use std::time::{Duration, Instant};

    async fn local_server() -> (tokio::net::TcpListener, AgentConfig) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let cfg = AgentConfig { server_url: format!("http://{}", listener.local_addr().unwrap()), ..AgentConfig::default() };
        (listener, cfg)
    }

    #[tokio::test]
    async fn end_follows_all_queued_audio() {
        let (listener, cfg) = local_server().await;
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut ws = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let mut frames = 0;
            while let Some(Ok(msg)) = ws.next().await {
                match msg {
                    Message::Binary(_) => frames += 1,
                    Message::Text(t) if t.as_str().contains(r#""end""#) => return frames,
                    _ => {}
                }
            }
            panic!("no end");
        });
        let session = DictateSession::open(&cfg, None, None).await.unwrap();
        let tx = session.audio_tx_clone();
        for _ in 0..200 {
            tx.send(vec![0i16; 320]).await.unwrap();
        }
        session.end_after(async {});
        assert_eq!(server.await.unwrap(), 200);
    }

    /// A server that stops reading: the queue fills, the pump blocks, and a cancel
    /// must still release it instead of leaving it stuck forever.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn cancel_releases_a_pump_stuck_on_a_stalled_connection() {
        let (listener, cfg) = local_server().await;
        let _server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let _ws = tokio_tungstenite::accept_async(tcp).await.unwrap();
            tokio::time::sleep(Duration::from_secs(60)).await;
        });
        let session = DictateSession::open(&cfg, None, None).await.unwrap();
        let tx = session.audio_tx_clone();
        let pump = tokio::task::spawn_blocking(move || while tx.blocking_send(vec![0i16; 32_000]).is_ok() {});
        tokio::time::sleep(Duration::from_millis(500)).await;
        assert!(!pump.is_finished(), "the stalled connection should have blocked the pump");
        session.end_after(async {});
        let started = Instant::now();
        session.cancel().await;
        tokio::time::timeout(CANCEL_GRACE + Duration::from_secs(3), pump).await.expect("pump still blocked after cancel").unwrap();
        assert!(started.elapsed() >= CANCEL_GRACE - Duration::from_millis(100));
    }
}
