use std::path::PathBuf;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct AgentConfig {
    /// e.g. http://127.0.0.1:7411
    pub server_url: String,
    pub token: String,
    pub profile: Option<String>,
    pub language: Option<String>,
    /// e.g. "ctrl+alt+space", "rightalt", "scrolllock", "f13".
    pub hotkey: String,
    /// Hotkey for "I fixed the text, learn from my selection".
    pub correction_hotkey: Option<String>,
    /// "hold" = record while pressed, "toggle" = press to start, press to stop.
    pub hotkey_mode: HotkeyMode,
    /// Inject the final text automatically (CLI mode, or client that just displays).
    pub auto_inject: bool,
    pub inject: InjectConfig,
    pub mic_device: Option<String>,
    /// Linux: PulseAudio/PipeWire source name of the sink monitor; Windows: output device name.
    pub loopback_device: Option<String>,
    pub partials: bool,
    /// Where meeting WAVs go. Default: platform data dir / transcriptd / meetings.
    pub meetings_dir: Option<PathBuf>,
    /// Name used for our virtual keyboard so the hotkey listener ignores it.
    pub virtual_device_name: String,
    /// How long to wait for the server's transcript after a take ends before giving up.
    pub final_timeout_secs: u64,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "snake_case")]
pub enum HotkeyMode {
    #[default]
    Hold,
    Toggle,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct InjectConfig {
    /// "accessibility" (insert through AT-SPI / UI Automation, clipboard fallback),
    /// "clipboard" (paste chord) or "type" (Windows unicode typing; Linux falls back to clipboard).
    pub method: InjectMethod,
    /// "shift+insert" | "ctrl+v" | "ctrl+shift+v". Linux default shift+insert (layout independent).
    pub paste_chord: Option<String>,
    /// Restore the previous clipboard contents after pasting.
    pub restore_clipboard: bool,
    /// How long to wait for the target app to consume the paste before restoring, in ms.
    pub restore_delay_ms: u64,
    /// Append a trailing space after injected text.
    pub trailing_space: bool,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "snake_case")]
pub enum InjectMethod {
    /// Insert at the caret through the accessibility layer; falls back to clipboard
    /// when the focused control cannot be reached that way.
    #[default]
    Accessibility,
    Clipboard,
    Type,
}

impl Default for InjectConfig {
    fn default() -> Self {
        Self {
            method: InjectMethod::Accessibility,
            paste_chord: None,
            restore_clipboard: true,
            restore_delay_ms: 400,
            trailing_space: true,
        }
    }
}

impl Default for AgentConfig {
    fn default() -> Self {
        Self {
            server_url: "http://127.0.0.1:7411".into(),
            token: String::new(),
            profile: None,
            language: None,
            hotkey: "ctrl+alt+space".into(),
            correction_hotkey: Some("ctrl+alt+shift+v".into()),
            hotkey_mode: HotkeyMode::Hold,
            auto_inject: true,
            inject: InjectConfig::default(),
            mic_device: None,
            loopback_device: None,
            partials: false,
            meetings_dir: None,
            virtual_device_name: "transcriptd virtual keyboard".into(),
            final_timeout_secs: 60,
        }
    }
}

impl AgentConfig {
    pub fn config_path() -> Option<PathBuf> {
        directories::ProjectDirs::from("", "", "transcriptd").map(|d| d.config_dir().join("agent.json"))
    }

    pub fn load() -> Self {
        let Some(p) = Self::config_path() else { return Self::default() };
        match std::fs::read(&p) {
            Ok(bytes) => serde_json::from_slice(&bytes).unwrap_or_else(|e| {
                tracing::warn!("bad config at {}: {e}; using defaults", p.display());
                Self::default()
            }),
            Err(_) => Self::default(),
        }
    }

    /// Write the config (it holds the server token) readable by the owner only.
    pub fn save(&self) -> anyhow::Result<()> {
        let p = Self::config_path().ok_or_else(|| anyhow::anyhow!("no config dir"))?;
        let dir = p.parent().unwrap();
        create_private_dir(dir)?;
        // Write a fresh 0600 file and rename it over the old one, so neither a crash nor
        // the old file's permissions can leave the token world-readable.
        let tmp = p.with_extension("json.tmp");
        let _ = std::fs::remove_file(&tmp);
        {
            use std::io::Write;
            let mut opts = std::fs::OpenOptions::new();
            opts.write(true).create_new(true);
            #[cfg(unix)]
            std::os::unix::fs::OpenOptionsExt::mode(&mut opts, 0o600);
            let mut f = opts.open(&tmp)?;
            f.write_all(&serde_json::to_vec_pretty(self)?)?;
            f.sync_all()?;
        }
        std::fs::rename(&tmp, &p)?;
        Ok(())
    }

    pub fn meetings_dir(&self) -> PathBuf {
        self.meetings_dir.clone().unwrap_or_else(|| {
            directories::ProjectDirs::from("", "", "transcriptd")
                .map(|d| d.data_dir().join("meetings"))
                .unwrap_or_else(|| PathBuf::from("meetings"))
        })
    }

    /// WebSocket URL for `path`: `https://` maps to `wss://`, `http://` (or no scheme) to `ws://`.
    pub fn ws_url(&self, path: &str) -> String {
        let (secure, rest) = split_scheme(&self.server_url);
        format!("{}://{rest}{path}", if secure { "wss" } else { "ws" })
    }

    /// HTTP(S) URL for `path`; a server URL without a scheme is taken as `http://`.
    pub fn http_url(&self, path: &str) -> String {
        let (secure, rest) = split_scheme(&self.server_url);
        format!("{}://{rest}{path}", if secure { "https" } else { "http" })
    }
}

/// `(is TLS, rest after the scheme without a trailing slash)`. Accepts http/https and
/// ws/wss in any case; anything else is treated as a bare `host:port`.
fn split_scheme(url: &str) -> (bool, &str) {
    let url = url.trim().trim_end_matches('/');
    for (scheme, secure) in [("https://", true), ("wss://", true), ("http://", false), ("ws://", false)] {
        if url.len() >= scheme.len() && url.is_char_boundary(scheme.len()) && url[..scheme.len()].eq_ignore_ascii_case(scheme) {
            return (secure, &url[scheme.len()..]);
        }
    }
    (false, url)
}

/// Create `dir` (and parents) and make it accessible to the owner only on Unix.
pub fn create_private_dir(dir: &std::path::Path) -> std::io::Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
        std::fs::DirBuilder::new().recursive(true).mode(0o700).create(dir)?;
        std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700))
    }
    #[cfg(not(unix))]
    {
        std::fs::create_dir_all(dir)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg(url: &str) -> AgentConfig {
        AgentConfig { server_url: url.into(), ..AgentConfig::default() }
    }

    #[test]
    fn server_url_maps_to_ws_and_http() {
        assert_eq!(cfg("http://127.0.0.1:7411").ws_url("/v1/dictate"), "ws://127.0.0.1:7411/v1/dictate");
        assert_eq!(cfg("https://box.example.net/").ws_url("/v1/dictate"), "wss://box.example.net/v1/dictate");
        assert_eq!(cfg("HTTPS://box.example.net:8443").ws_url("/x"), "wss://box.example.net:8443/x");
        assert_eq!(cfg("127.0.0.1:7411").ws_url("/x"), "ws://127.0.0.1:7411/x");
        assert_eq!(cfg("wss://box.example.net").ws_url("/x"), "wss://box.example.net/x");
        assert_eq!(cfg(" https://box.example.net/base/ ").ws_url("/x"), "wss://box.example.net/base/x");
        assert_eq!(cfg("https://box.example.net").http_url("/v1/meetings"), "https://box.example.net/v1/meetings");
        assert_eq!(cfg("http://127.0.0.1:7411/").http_url("/v1/meetings"), "http://127.0.0.1:7411/v1/meetings");
        assert_eq!(cfg("127.0.0.1:7411").http_url("/v1/meetings"), "http://127.0.0.1:7411/v1/meetings");
        assert_eq!(cfg("wss://box.example.net").http_url("/v1"), "https://box.example.net/v1");
    }

    #[cfg(unix)]
    #[test]
    fn private_dir_is_owner_only() {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir().join(format!("transcriptd-agent-test-{}", std::process::id())).join("a");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o755)).unwrap();
        create_private_dir(&dir).unwrap();
        assert_eq!(std::fs::metadata(&dir).unwrap().permissions().mode() & 0o777, 0o700);
        let _ = std::fs::remove_dir_all(dir.parent().unwrap());
    }
}
