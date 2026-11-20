mod app;
mod audio;
mod config;
mod hotkey;
mod inject;
mod ipc;
mod protocol;
mod readback;
mod server;

use anyhow::Result;
use clap::{Parser, Subcommand};

use config::AgentConfig;

#[derive(Parser)]
#[command(name = "transcriptd-agent", version, about = "transcriptd sidecar: push-to-talk, audio capture, text injection")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
    /// Server base URL (overrides saved config)
    #[arg(long, global = true)]
    server: Option<String>,
    /// API token (overrides saved config)
    #[arg(long, global = true, env = "TRANSCRIPTD_TOKEN")]
    token: Option<String>,
    #[arg(long, global = true)]
    profile: Option<String>,
}

#[derive(Subcommand)]
enum Cmd {
    /// Run as a sidecar: JSON-lines commands on stdin, events on stdout.
    Run {
        /// Do not read commands from stdin (headless daemon controlled by hotkeys + `toggle`).
        #[arg(long)]
        no_stdin: bool,
        /// Push-to-talk hotkey, e.g. "ctrl+alt+space" or "rightalt".
        #[arg(long)]
        hotkey: Option<String>,
        /// "hold" or "toggle"
        #[arg(long)]
        mode: Option<String>,
        /// Do not inject the result, only print it.
        #[arg(long)]
        no_inject: bool,
    },
    /// Tell the running agent to start/stop a recording (for compositor keybindings).
    Toggle,
    /// Tell the running agent to inject text.
    Inject { text: String },
    /// Start/stop a meeting recording on the running agent.
    Meeting {
        #[arg(value_parser = ["start", "stop"])]
        action: String,
        #[arg(long)]
        title: Option<String>,
    },
    /// List audio devices.
    Devices,
    /// Print the saved config path and contents.
    Config,
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env().add_directive("info".parse()?))
        .with_writer(std::io::stderr)
        .init();
    // rustls needs a process-wide crypto provider for anything that does not pass its own.
    let _ = rustls::crypto::ring::default_provider().install_default();
    let cli = Cli::parse();
    let mut cfg = AgentConfig::load();
    if let Some(s) = cli.server {
        cfg.server_url = s;
    }
    if let Some(t) = cli.token {
        cfg.token = t;
    }
    if let Some(p) = cli.profile {
        cfg.profile = Some(p);
    }
    match cli.cmd {
        Cmd::Run { no_stdin, hotkey, mode, no_inject } => {
            if let Some(h) = hotkey {
                cfg.hotkey = h;
            }
            if let Some(m) = mode {
                cfg.hotkey_mode = if m == "toggle" { config::HotkeyMode::Toggle } else { config::HotkeyMode::Hold };
            }
            if no_inject {
                cfg.auto_inject = false;
            }
            app::run(cfg, !no_stdin).await
        }
        Cmd::Toggle => ipc::send_to_instance(&ipc::RemoteCommand::PttToggle).await,
        Cmd::Inject { text } => ipc::send_to_instance(&ipc::RemoteCommand::Inject { text }).await,
        Cmd::Meeting { action, title } => {
            let c = if action == "start" { ipc::RemoteCommand::MeetingStart { meeting_id: None, title } } else { ipc::RemoteCommand::MeetingStop };
            ipc::send_to_instance(&c).await
        }
        Cmd::Devices => {
            let (i, o) = audio::mic::list();
            println!("inputs:");
            for d in i {
                println!("  {}{}", d.name, if d.default { "  (default)" } else { "" });
            }
            println!("outputs:");
            for d in o {
                println!("  {}{}", d.name, if d.default { "  (default)" } else { "" });
            }
            #[cfg(target_os = "linux")]
            if let Ok(m) = audio::loopback::default_sink_monitor() {
                println!("loopback (default sink monitor): {m}");
            }
            Ok(())
        }
        Cmd::Config => {
            println!("{}", AgentConfig::config_path().map(|p| p.display().to_string()).unwrap_or_default());
            println!("{}", serde_json::to_string_pretty(&cfg)?);
            Ok(())
        }
    }
}
