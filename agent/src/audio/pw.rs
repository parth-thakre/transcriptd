//! Linux audio via PipeWire/PulseAudio command line tools as child processes.
//!
//! `pw-record` (or `parec`) gives us s16le mono 16 kHz on stdout for any source,
//! including sink monitors, on every desktop, with no build-time dependencies and no
//! portal. Device enumeration goes through `pactl`.

use std::io::Read;
use std::process::{Child, Command, Stdio};

use anyhow::{anyhow, Context, Result};
use crossbeam_channel::bounded;

use super::{Capture, Framer, RATE};
use crate::protocol::DeviceInfo;

pub fn which(bin: &str) -> bool {
    std::env::var_os("PATH")
        .map(|p| std::env::split_paths(&p).any(|d| d.join(bin).is_file()))
        .unwrap_or(false)
}

fn pactl(args: &[&str]) -> Result<String> {
    let out = Command::new("pactl").args(args).output().with_context(|| format!("pactl {}", args.join(" ")))?;
    if !out.status.success() {
        return Err(anyhow!("pactl {} failed: {}", args.join(" "), String::from_utf8_lossy(&out.stderr).trim()));
    }
    Ok(String::from_utf8_lossy(&out.stdout).to_string())
}

/// The default source name, or None when PipeWire only reports a placeholder
/// (`@DEFAULT_SOURCE@`), in which case pw-record's own "auto" target is used.
pub fn default_source() -> Result<Option<String>> {
    let s = pactl(&["get-default-source"])?.trim().to_string();
    if s.is_empty() || s.starts_with('@') {
        return Ok(None);
    }
    Ok(Some(s))
}

pub fn default_sink_monitor() -> Result<String> {
    let s = pactl(&["get-default-sink"])?.trim().to_string();
    if s.is_empty() {
        return Err(anyhow!("no default sink"));
    }
    Ok(format!("{s}.monitor"))
}

/// (inputs = non-monitor sources, outputs = sinks). Names are PulseAudio/PipeWire node names.
pub fn list() -> (Vec<DeviceInfo>, Vec<DeviceInfo>) {
    let def_src = default_source().ok().flatten();
    let def_sink = pactl(&["get-default-sink"]).map(|s| s.trim().to_string()).ok();
    let mut inputs = Vec::new();
    if let Ok(out) = pactl(&["list", "short", "sources"]) {
        for line in out.lines() {
            if let Some(name) = line.split('\t').nth(1) {
                if name.ends_with(".monitor") {
                    continue;
                }
                inputs.push(DeviceInfo { default: Some(name) == def_src.as_deref(), name: name.to_string() });
            }
        }
    }
    let mut outputs = Vec::new();
    if let Ok(out) = pactl(&["list", "short", "sinks"]) {
        for line in out.lines() {
            if let Some(name) = line.split('\t').nth(1) {
                outputs.push(DeviceInfo { default: Some(name) == def_sink.as_deref(), name: name.to_string() });
            }
        }
    }
    (inputs, outputs)
}

/// Record `target` as 16 kHz mono i16 frames. `target` is a source node name, a
/// `<sink>.monitor` name (captures that sink's monitor), or None for the default source.
pub fn record(target: Option<&str>) -> Result<Capture> {
    let label = target.unwrap_or("default source").to_string();
    let mut child: Child = if which("pw-record") {
        let mut cmd = Command::new("pw-record");
        match target {
            Some(t) if t.ends_with(".monitor") => {
                // PipeWire: capture a sink's monitor by targeting the sink node with
                // stream.capture.sink=true; the ".monitor" pseudo-name is Pulse-only.
                cmd.args(["-P", "{ stream.capture.sink = true }", "--target", t.trim_end_matches(".monitor")]);
            }
            Some(t) => {
                cmd.args(["--target", t]);
            }
            None => {}
        }
        cmd.args(["--rate", &RATE.to_string(), "--channels", "1", "--format", "s16", "--latency", "20ms", "-"])
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .context("spawn pw-record")?
    } else if which("parec") {
        let target = target.unwrap_or("@DEFAULT_SOURCE@");
        Command::new("parec")
            .args(["-d", target, &format!("--rate={RATE}"), "--channels=1", "--format=s16le", "--raw", "--latency-msec=20"])
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .context("spawn parec")?
    } else {
        return Err(anyhow!("neither pw-record nor parec found; install pipewire-utils or pulseaudio-utils"));
    };
    let mut stdout = child.stdout.take().unwrap();
    let (tx, rx) = bounded::<Vec<i16>>(256);
    std::thread::Builder::new().name(format!("pw-{label}")).spawn(move || {
        let mut buf = vec![0u8; 1280]; // 20 ms of s16 mono at 16 kHz
        let mut framer = Framer::new();
        loop {
            match stdout.read(&mut buf) {
                Ok(0) | Err(_) => break,
                Ok(n) => {
                    let samples: Vec<i16> = buf[..n - n % 2].chunks_exact(2).map(|c| i16::from_le_bytes([c[0], c[1]])).collect();
                    for frame in framer.push(&samples) {
                        if tx.send(frame).is_err() {
                            return;
                        }
                    }
                }
            }
        }
    })?;
    tracing::info!("capture on {label}");
    Ok(Capture::new(rx, label, Box::new(KillOnDrop(child))))
}

struct KillOnDrop(Child);
impl Drop for KillOnDrop {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
