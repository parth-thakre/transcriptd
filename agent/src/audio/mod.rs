//! Audio capture. Everything downstream gets mono 16 kHz i16 frames.

pub mod loopback;
pub mod mic;
#[cfg(target_os = "linux")]
pub mod pw;
#[cfg_attr(not(windows), allow(dead_code))]
pub mod resample;
pub mod wav;

use crossbeam_channel::Receiver;

pub const RATE: u32 = 16_000;
/// 20 ms frames.
pub const FRAME: usize = (RATE / 50) as usize;

/// A running capture: frames arrive on `rx`; drop the handle to stop.
pub struct Capture {
    pub rx: Receiver<Vec<i16>>,
    #[allow(dead_code)]
    pub name: String,
    _stop: Box<dyn std::any::Any + Send>,
}

impl Capture {
    pub fn new(rx: Receiver<Vec<i16>>, name: String, stop: Box<dyn std::any::Any + Send>) -> Self {
        Self { rx, name, _stop: stop }
    }
}

pub fn rms_db(frame: &[i16]) -> f32 {
    if frame.is_empty() {
        return -120.0;
    }
    let sum: f64 = frame.iter().map(|&s| (s as f64 / 32768.0).powi(2)).sum();
    let rms = (sum / frame.len() as f64).sqrt().max(1e-9);
    (20.0 * rms.log10()) as f32
}

/// Accumulates arbitrary-length mono i16 chunks into fixed 20 ms frames.
pub struct Framer {
    buf: Vec<i16>,
}

impl Framer {
    pub fn new() -> Self {
        Self { buf: Vec::with_capacity(FRAME * 4) }
    }
    pub fn push(&mut self, samples: &[i16]) -> Vec<Vec<i16>> {
        self.buf.extend_from_slice(samples);
        let mut out = Vec::new();
        while self.buf.len() >= FRAME {
            out.push(self.buf.drain(..FRAME).collect());
        }
        out
    }
}
