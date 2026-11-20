//! System ("what the other people say") audio capture.
//!
//! Windows: WASAPI loopback; cpal opens a render endpoint as an input stream.
//! Linux: the default sink's monitor through pw-record/parec (pw.rs). Below the
//! compositor, no portal, no permission prompt, works on every desktop.

use anyhow::Result;

use super::Capture;

#[cfg(windows)]
pub fn start(device: Option<&str>) -> Result<Capture> {
    use anyhow::anyhow;
    use cpal::traits::HostTrait;

    use super::mic::dev_name;

    let host = cpal::default_host();
    let dev = match device {
        None | Some("") | Some("default") => host.default_output_device().ok_or_else(|| anyhow!("no default output device"))?,
        Some(n) => host
            .output_devices()?
            .find(|d| dev_name(d).as_deref() == Some(n))
            .ok_or_else(|| anyhow!("output device {n:?} not found"))?,
    };
    // cpal >= 0.15 builds a loopback capture when build_input_stream is called on a
    // render device. Loopback delivers nothing while the endpoint is idle, which is
    // fine: the meeting app is playing audio whenever there is something to capture.
    super::mic::open_input(dev)
}

#[cfg(target_os = "linux")]
pub fn start(device: Option<&str>) -> Result<Capture> {
    let target = match device {
        None | Some("") | Some("default") => super::pw::default_sink_monitor()?,
        Some(n) if n.ends_with(".monitor") => n.to_string(),
        Some(n) => format!("{n}.monitor"),
    };
    super::pw::record(Some(&target))
}

#[cfg(target_os = "linux")]
pub fn default_sink_monitor() -> Result<String> {
    super::pw::default_sink_monitor()
}

#[cfg(not(any(windows, target_os = "linux")))]
pub fn start(_device: Option<&str>) -> Result<Capture> {
    Err(anyhow::anyhow!("loopback capture not supported on this platform"))
}
