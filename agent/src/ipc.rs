//! Single-instance control socket so `transcriptd-agent toggle` (bound to a compositor
//! keybinding) reaches the running agent.
//!
//! The socket is a second, unauthenticated way in, so it accepts only what the CLI
//! sends ([`RemoteCommand`]): recording control, `inject`, and meeting start/stop.
//! Configuration (server URL, token), clipboard/selection reads and `quit` stay on the
//! client's stdin. On Unix the socket lives in a directory only the user can enter and
//! is itself 0600; on Windows the pipe is created as the first instance (so a squatter
//! makes us fail instead of us talking to it) and refuses remote clients.

use anyhow::Result;
use serde::{Deserialize, Serialize};
use tokio::sync::mpsc;

use crate::protocol::Command;

/// The commands the control socket accepts, and the CLI sends.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum RemoteCommand {
    #[serde(rename = "ptt.toggle")]
    PttToggle,
    #[serde(rename = "ptt.start")]
    PttStart,
    #[serde(rename = "ptt.stop")]
    PttStop,
    #[serde(rename = "ptt.cancel")]
    PttCancel,
    #[serde(rename = "inject")]
    Inject { text: String },
    #[serde(rename = "meeting.start")]
    MeetingStart {
        #[serde(default)]
        meeting_id: Option<i64>,
        #[serde(default)]
        title: Option<String>,
    },
    #[serde(rename = "meeting.stop")]
    MeetingStop,
}

impl From<RemoteCommand> for Command {
    fn from(c: RemoteCommand) -> Self {
        match c {
            RemoteCommand::PttToggle => Command::PttToggle,
            RemoteCommand::PttStart => Command::PttStart,
            RemoteCommand::PttStop => Command::PttStop,
            RemoteCommand::PttCancel => Command::PttCancel,
            RemoteCommand::Inject { text } => Command::Inject { text },
            RemoteCommand::MeetingStart { meeting_id, title } => Command::MeetingStart { meeting_id, title },
            RemoteCommand::MeetingStop => Command::MeetingStop,
        }
    }
}

/// Parse one line from the socket; anything outside [`RemoteCommand`] is refused.
fn parse_remote(line: &str) -> Option<Command> {
    match serde_json::from_str::<RemoteCommand>(line) {
        Ok(c) => Some(c.into()),
        Err(e) => {
            tracing::warn!("control socket: refused command: {e}");
            None
        }
    }
}

async fn serve_lines<S: tokio::io::AsyncRead + Unpin>(stream: S, tx: mpsc::Sender<Command>) {
    use tokio::io::{AsyncBufReadExt, BufReader};
    let mut lines = BufReader::new(stream).lines();
    while let Ok(Some(line)) = lines.next_line().await {
        if line.trim().is_empty() {
            continue;
        }
        if let Some(cmd) = parse_remote(&line) {
            if tx.send(cmd).await.is_err() {
                break;
            }
        }
    }
}

/// Directory for the socket: `$XDG_RUNTIME_DIR` (per-user, 0700 by spec), else a
/// per-user directory under the cache dir. Never a fixed name in shared /tmp.
/// Returns `(dir, ours)`; `ours` means we create it and keep it 0700.
#[cfg(unix)]
fn socket_dir() -> (std::path::PathBuf, bool) {
    if let Some(dir) = std::env::var_os("XDG_RUNTIME_DIR").map(std::path::PathBuf::from).filter(|d| d.is_absolute()) {
        return (dir, false);
    }
    let dir = directories::ProjectDirs::from("", "", "transcriptd")
        .map(|d| d.cache_dir().join("run"))
        .unwrap_or_else(|| std::path::PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".transcriptd-run"));
    (dir, true)
}

#[cfg(unix)]
pub fn socket_path() -> std::path::PathBuf {
    socket_dir().0.join("transcriptd-agent.sock")
}

#[cfg(windows)]
pub const PIPE_NAME: &str = r"\\.\pipe\transcriptd-agent";

/// Bind the control socket. Fails when another agent is already running.
#[cfg(unix)]
pub async fn serve_single_instance(tx: mpsc::Sender<Command>) -> Result<()> {
    let (dir, ours) = socket_dir();
    if ours {
        crate::config::create_private_dir(&dir)?;
    }
    let lock = lock_instance(&dir.join("transcriptd-agent.lock"))?;
    // Held (open, locked) until the process exits.
    INSTANCE_LOCK.get_or_init(|| lock);
    let listener = bind_control_socket(&socket_path()).await?;
    tokio::spawn(async move {
        loop {
            let Ok((stream, _)) = listener.accept().await else { break };
            tokio::spawn(serve_lines(stream, tx.clone()));
        }
    });
    Ok(())
}

#[cfg(unix)]
static INSTANCE_LOCK: std::sync::OnceLock<std::fs::File> = std::sync::OnceLock::new();

/// Take the single-instance lock: an exclusive `flock` on a file next to the socket.
/// Only its holder may probe, unlink and bind the socket, so two agents starting after
/// a crash cannot both find the socket stale and remove each other's fresh listener.
/// The file is never removed (unlinking a held lock would let a third agent lock a new
/// file of the same name); the kernel drops the lock when the process exits.
#[cfg(unix)]
fn lock_instance(path: &std::path::Path) -> Result<std::fs::File> {
    use std::os::unix::fs::OpenOptionsExt;
    let file = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(path)
        .map_err(|e| anyhow::Error::new(e).context(format!("open lock file {}", path.display())))?;
    match file.try_lock() {
        Ok(()) => Ok(file),
        Err(std::fs::TryLockError::WouldBlock) => {
            anyhow::bail!("another transcriptd-agent is already running (lock {} is held)", path.display())
        }
        Err(std::fs::TryLockError::Error(e)) => Err(anyhow::Error::new(e).context(format!("lock {}", path.display()))),
    }
}

/// Bind `path`, replacing a stale socket left by a crashed agent. Call only while
/// holding the instance lock.
#[cfg(unix)]
async fn bind_control_socket(path: &std::path::Path) -> Result<tokio::net::UnixListener> {
    use std::os::unix::fs::{FileTypeExt, PermissionsExt};
    match std::fs::symlink_metadata(path) {
        Ok(meta) => {
            anyhow::ensure!(meta.file_type().is_socket(), "{} exists and is not a socket; not touching it", path.display());
            // A live listener means another agent owns it (one that predates the lock
            // file); only a dead one may be replaced.
            match tokio::net::UnixStream::connect(path).await {
                Ok(_) => anyhow::bail!("another transcriptd-agent is already running (control socket {})", path.display()),
                Err(e) if e.kind() == std::io::ErrorKind::ConnectionRefused => {
                    tracing::info!("removing stale control socket {}", path.display());
                    std::fs::remove_file(path)?;
                }
                Err(e) => return Err(anyhow::Error::new(e).context(format!("probe control socket {}", path.display()))),
            }
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(e.into()),
    }
    let listener = tokio::net::UnixListener::bind(path)?;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))?;
    Ok(listener)
}

/// Remove our socket on a clean exit (a leftover one is detected as stale anyway).
#[cfg(unix)]
pub fn cleanup() {
    let _ = std::fs::remove_file(socket_path());
}

#[cfg(windows)]
pub fn cleanup() {}

#[cfg(unix)]
pub async fn send_to_instance(cmd: &RemoteCommand) -> Result<()> {
    use anyhow::Context;
    use tokio::io::AsyncWriteExt;
    let path = socket_path();
    let mut s = tokio::net::UnixStream::connect(&path)
        .await
        .with_context(|| format!("no running agent at {}", path.display()))?;
    s.write_all(format!("{}\n", serde_json::to_string(cmd)?).as_bytes()).await?;
    Ok(())
}

/// Create the pipe. The first instance must be ours: if the name is taken (another
/// agent, or someone squatting it), fail instead of serving or connecting to it.
#[cfg(windows)]
pub async fn serve_single_instance(tx: mpsc::Sender<Command>) -> Result<()> {
    use anyhow::Context;
    use tokio::net::windows::named_pipe::ServerOptions;

    let mut server = ServerOptions::new()
        .first_pipe_instance(true)
        .reject_remote_clients(true)
        .create(PIPE_NAME)
        .context("another transcriptd-agent is already running (control pipe in use)")?;
    tokio::spawn(async move {
        loop {
            if server.connect().await.is_err() {
                server = match ServerOptions::new().reject_remote_clients(true).create(PIPE_NAME) {
                    Ok(s) => s,
                    Err(_) => break,
                };
                continue;
            }
            // Hand this instance to a reader and open the next one for the next client.
            let next = match ServerOptions::new().reject_remote_clients(true).create(PIPE_NAME) {
                Ok(s) => s,
                Err(_) => break,
            };
            let connected = std::mem::replace(&mut server, next);
            tokio::spawn(serve_lines(connected, tx.clone()));
        }
    });
    Ok(())
}

#[cfg(windows)]
pub async fn send_to_instance(cmd: &RemoteCommand) -> Result<()> {
    use tokio::io::AsyncWriteExt;
    use tokio::net::windows::named_pipe::ClientOptions;
    let mut c = ClientOptions::new().open(PIPE_NAME)?;
    c.write_all(format!("{}\n", serde_json::to_string(cmd)?).as_bytes()).await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn socket_accepts_only_cli_commands() {
        for line in [
            r#"{"type":"ptt.toggle"}"#,
            r#"{"type":"ptt.cancel"}"#,
            r#"{"type":"inject","text":"hi"}"#,
            r#"{"type":"meeting.start","meeting_id":null,"title":"standup"}"#,
            r#"{"type":"meeting.start"}"#,
            r#"{"type":"meeting.stop"}"#,
        ] {
            assert!(parse_remote(line).is_some(), "{line}");
        }
        for line in [
            r#"{"type":"configure","config":{"server_url":"http://evil.example","token":"x"}}"#,
            r#"{"type":"clipboard.read"}"#,
            r#"{"type":"selection.read"}"#,
            r#"{"type":"devices.list"}"#,
            r#"{"type":"quit"}"#,
            r#"{"type":"ping"}"#,
            "not json",
        ] {
            assert!(parse_remote(line).is_none(), "{line}");
        }
    }

    #[cfg(unix)]
    fn scratch_dir(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("transcriptd-agent-test-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[cfg(unix)]
    #[test]
    fn instance_lock_is_exclusive() {
        use std::os::unix::fs::PermissionsExt;
        let dir = scratch_dir("lock");
        let path = dir.join("transcriptd-agent.lock");
        let first = lock_instance(&path).unwrap();
        assert_eq!(std::fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
        // flock is per open file, so a second open in this process conflicts like a
        // second agent would.
        let err = lock_instance(&path).unwrap_err().to_string();
        assert!(err.contains("already running"), "{err}");
        drop(first);
        lock_instance(&path).unwrap();
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn stale_socket_replaced_live_one_kept() {
        let dir = scratch_dir("sock");
        let path = dir.join("transcriptd-agent.sock");
        // A crashed agent leaves its socket file behind with nobody listening.
        drop(std::os::unix::net::UnixListener::bind(&path).unwrap());
        let live = bind_control_socket(&path).await.unwrap();
        let err = bind_control_socket(&path).await.unwrap_err().to_string();
        assert!(err.contains("already running"), "{err}");
        // The refused attempt left the live listener in place.
        tokio::net::UnixStream::connect(&path).await.unwrap();
        drop(live);
        std::fs::write(dir.join("plain"), b"").unwrap();
        assert!(bind_control_socket(&dir.join("plain")).await.is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn cli_commands_round_trip() {
        for cmd in [
            RemoteCommand::PttToggle,
            RemoteCommand::Inject { text: "hi".into() },
            RemoteCommand::MeetingStart { meeting_id: None, title: Some("t".into()) },
            RemoteCommand::MeetingStop,
        ] {
            let line = serde_json::to_string(&cmd).unwrap();
            assert_eq!(serde_json::from_str::<RemoteCommand>(&line).unwrap(), cmd);
        }
    }
}
