//! Two-channel 16 kHz WAV writer for meeting backups: left = me (mic), right = them.

use std::path::Path;

use anyhow::Result;
use hound::{SampleFormat, WavSpec, WavWriter};

use super::RATE;

pub struct StereoWav {
    w: WavWriter<std::io::BufWriter<std::fs::File>>,
    pub frames: u64,
}

impl StereoWav {
    pub fn create(path: &Path) -> Result<Self> {
        let spec = WavSpec { channels: 2, sample_rate: RATE, bits_per_sample: 16, sample_format: SampleFormat::Int };
        Ok(Self { w: WavWriter::create(path, spec)?, frames: 0 })
    }

    /// Write one 20 ms frame from each side (either may be silence if that side lagged).
    pub fn write(&mut self, me: &[i16], them: &[i16]) -> Result<()> {
        let n = me.len().max(them.len());
        for i in 0..n {
            self.w.write_sample(*me.get(i).unwrap_or(&0))?;
            self.w.write_sample(*them.get(i).unwrap_or(&0))?;
        }
        self.frames += n as u64;
        Ok(())
    }

    pub fn duration_ms(&self) -> u64 {
        self.frames * 1000 / RATE as u64
    }

    pub fn finish(self) -> Result<()> {
        self.w.finalize()?;
        Ok(())
    }
}
