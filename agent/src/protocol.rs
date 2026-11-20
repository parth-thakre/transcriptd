//! JSON-lines protocol between the Electron client (or CLI) and the agent.
//!
//! Commands arrive on stdin, one JSON object per line. Events go to stdout the same
//! way. Logs go to stderr. See protocol/agent-ipc.md for the reference.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Command {
    /// Replace the agent configuration (server URL, token, hotkey, injection...).
    Configure { config: crate::config::AgentConfig },
    /// Begin a push-to-talk recording as if the hotkey went down.
    #[serde(rename = "ptt.start")]
    PttStart,
    /// End the recording and run the final pass, as if the hotkey went up.
    #[serde(rename = "ptt.stop")]
    PttStop,
    /// Start if idle, stop if recording.
    #[serde(rename = "ptt.toggle")]
    PttToggle,
    /// Drop the current recording without transcribing.
    #[serde(rename = "ptt.cancel")]
    PttCancel,
    /// Put `text` into the focused application.
    Inject { text: String },
    /// Read the current clipboard (used by the correction hotkey flow).
    #[serde(rename = "clipboard.read")]
    ClipboardRead,
    /// Copy the current selection (sends the copy chord) and return it.
    #[serde(rename = "selection.read")]
    SelectionRead,
    #[serde(rename = "devices.list")]
    DevicesList,
    /// Record mic + system audio to a two-channel WAV and stream to the server.
    #[serde(rename = "meeting.start")]
    MeetingStart { meeting_id: Option<i64>, title: Option<String> },
    #[serde(rename = "meeting.stop")]
    MeetingStop,
    Ping,
    Quit,
}

#[allow(dead_code)]
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Event {
    Hello { version: String, platform: String, hotkey_backend: String, inject_backend: String, readback_backend: String },
    Configured { warnings: Vec<String> },
    #[serde(rename = "ptt.down")]
    PttDown { source: String },
    #[serde(rename = "ptt.up")]
    PttUp,
    /// RMS level of the mic in dBFS, a few times a second while recording.
    Level { db: f32 },
    Partial { text: String },
    /// The server's final result, passed through verbatim plus `injected`.
    Final { result: serde_json::Value, injected: bool },
    Injected { chars: usize, method: String },
    Clipboard { text: Option<String> },
    Selection { text: Option<String> },
    Devices { inputs: Vec<DeviceInfo>, outputs: Vec<DeviceInfo> },
    #[serde(rename = "meeting.started")]
    MeetingStarted { meeting_id: Option<i64>, wav_path: String },
    #[serde(rename = "meeting.level")]
    MeetingLevel { me_db: f32, them_db: f32 },
    #[serde(rename = "meeting.segment")]
    MeetingSegment { meeting_id: Option<i64>, segment: serde_json::Value },
    #[serde(rename = "meeting.stopped")]
    MeetingStopped { meeting_id: Option<i64>, wav_path: String, duration_ms: u64 },
    /// Word substitutions the user made in the target app after a paste (learned by
    /// reading the field back through the accessibility API). No UI involved.
    Corrections { dictation_id: Option<i64>, app: String, corrections: Vec<CorrectionPair> },
    Pong,
    Error { message: String, fatal: bool },
    /// A non-fatal `error` that also carries a machine-readable `code`; clients that
    /// only know `message`/`fatal` show it like any other error.
    #[serde(rename = "error")]
    Warning { message: String, fatal: bool, code: &'static str },
}

#[derive(Debug, Clone, Serialize)]
pub struct CorrectionPair {
    pub heard: String,
    pub meant: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct DeviceInfo {
    pub name: String,
    pub default: bool,
}
