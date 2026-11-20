//! Microphone capture. Linux: PipeWire tools (see pw.rs). Windows: cpal/WASAPI.

use anyhow::Result;

use super::Capture;
use crate::protocol::DeviceInfo;

#[cfg(target_os = "linux")]
pub fn list() -> (Vec<DeviceInfo>, Vec<DeviceInfo>) {
    super::pw::list()
}

#[cfg(target_os = "linux")]
pub fn start(device: Option<&str>) -> Result<Capture> {
    let target = match device {
        None | Some("") | Some("default") => super::pw::default_source()?,
        Some(n) => Some(n.to_string()),
    };
    super::pw::record(target.as_deref())
}

/// cpal 0.18: the human-readable name lives on the device description.
#[cfg(windows)]
pub(crate) fn dev_name(d: &cpal::Device) -> Option<String> {
    use cpal::traits::DeviceTrait;
    d.description().ok().map(|desc| desc.name().to_string())
}

#[cfg(windows)]
pub fn list() -> (Vec<DeviceInfo>, Vec<DeviceInfo>) {
    use cpal::traits::HostTrait;
    let host = cpal::default_host();
    let def_in = host.default_input_device().and_then(|d| dev_name(&d));
    let def_out = host.default_output_device().and_then(|d| dev_name(&d));
    let inputs = host
        .input_devices()
        .map(|it| {
            it.filter_map(|d| dev_name(&d))
                .map(|name| DeviceInfo { default: Some(&name) == def_in.as_ref(), name })
                .collect()
        })
        .unwrap_or_default();
    let outputs = host
        .output_devices()
        .map(|it| {
            it.filter_map(|d| dev_name(&d))
                .map(|name| DeviceInfo { default: Some(&name) == def_out.as_ref(), name })
                .collect()
        })
        .unwrap_or_default();
    (inputs, outputs)
}

#[cfg(windows)]
pub fn start(device: Option<&str>) -> Result<Capture> {
    use anyhow::anyhow;
    use cpal::traits::HostTrait;
    let host = cpal::default_host();
    let dev = match device {
        None | Some("") | Some("default") => host.default_input_device().ok_or_else(|| anyhow!("no default input device"))?,
        Some(n) => host
            .input_devices()?
            .find(|d| dev_name(d).as_deref() == Some(n))
            .ok_or_else(|| anyhow!("input device {n:?} not found"))?,
    };
    open_input(dev)
}

#[cfg(windows)]
pub(crate) fn open_input(dev: cpal::Device) -> Result<Capture> {
    use anyhow::{anyhow, Context};
    use cpal::traits::{DeviceTrait, StreamTrait};
    use crossbeam_channel::bounded;

    use super::resample::{downmix, f32_to_i16, Resampler};
    use super::{Framer, RATE};

    let name = dev_name(&dev).unwrap_or_else(|| "?".into());
    let cfg = dev.default_input_config().context("default input config")?;
    let src_rate = cfg.sample_rate();
    let channels = cfg.channels() as usize;
    let (tx, rx) = bounded::<Vec<i16>>(256);
    let mut resampler = Resampler::new(src_rate, RATE);
    let mut framer = Framer::new();
    let err_fn = |e| tracing::warn!("audio stream error: {e}");
    let stream_cfg: cpal::StreamConfig = cfg.clone().into();

    macro_rules! build {
        ($t:ty, $conv:expr) => {
            dev.build_input_stream(
                stream_cfg.clone(),
                move |data: &[$t], _| {
                    let f: Vec<f32> = data.iter().map($conv).collect();
                    let mono = downmix(&f, channels);
                    let rs = resampler.process(&mono);
                    for frame in framer.push(&f32_to_i16(&rs)) {
                        let _ = tx.try_send(frame);
                    }
                },
                err_fn,
                None,
            )?
        };
    }
    let stream = match cfg.sample_format() {
        cpal::SampleFormat::F32 => build!(f32, |&v| v),
        cpal::SampleFormat::I16 => build!(i16, |&v| v as f32 / 32768.0),
        cpal::SampleFormat::U16 => build!(u16, |&v| (v as f32 - 32768.0) / 32768.0),
        cpal::SampleFormat::I32 => build!(i32, |&v| v as f32 / 2147483648.0),
        other => return Err(anyhow!("unsupported sample format {other:?}")),
    };
    stream.play()?;
    tracing::info!("mic capture on {name} @ {src_rate} Hz x{channels}");
    Ok(Capture::new(rx, name, Box::new(SendStream(stream))))
}

/// cpal::Stream is !Send on some hosts; the handle only ever lives on the app thread.
#[cfg(windows)]
pub(crate) struct SendStream(pub cpal::Stream);
#[cfg(windows)]
unsafe impl Send for SendStream {}

#[cfg(not(any(target_os = "linux", windows)))]
pub fn list() -> (Vec<DeviceInfo>, Vec<DeviceInfo>) {
    (vec![], vec![])
}
#[cfg(not(any(target_os = "linux", windows)))]
pub fn start(_device: Option<&str>) -> Result<Capture> {
    Err(anyhow::anyhow!("no microphone backend for this platform"))
}
