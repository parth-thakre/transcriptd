//! Small, dependency-free resampler for speech: windowed-sinc low-pass when
//! downsampling, then linear interpolation. Good enough for 44.1/48 kHz -> 16 kHz.

pub struct Resampler {
    src: u32,
    dst: u32,
    taps: Vec<f32>,
    history: Vec<f32>, // last taps-1 input samples
    pos: f64,          // fractional read position in the filtered stream
}

impl Resampler {
    pub fn new(src: u32, dst: u32) -> Self {
        let taps = if src > dst { lowpass(dst as f32 * 0.45 / src as f32, 31) } else { vec![1.0] };
        Self { src, dst, history: vec![0.0; taps.len().saturating_sub(1)], taps, pos: 0.0 }
    }

    pub fn identity(&self) -> bool {
        self.src == self.dst
    }

    pub fn process(&mut self, input: &[f32]) -> Vec<f32> {
        if self.src == self.dst {
            return input.to_vec();
        }
        // Filter.
        let n = self.taps.len();
        let mut ext = Vec::with_capacity(self.history.len() + input.len());
        ext.extend_from_slice(&self.history);
        ext.extend_from_slice(input);
        let filtered: Vec<f32> = if n > 1 {
            (0..input.len())
                .map(|i| self.taps.iter().enumerate().map(|(k, t)| t * ext[i + n - 1 - k]).sum())
                .collect()
        } else {
            input.to_vec()
        };
        if n > 1 {
            let keep = n - 1;
            let start = ext.len().saturating_sub(keep);
            self.history = ext[start..].to_vec();
        }
        // Interpolate.
        let ratio = self.src as f64 / self.dst as f64;
        let mut out = Vec::with_capacity((input.len() as f64 / ratio) as usize + 2);
        let len = filtered.len();
        while self.pos + 1.0 < len as f64 {
            let i = self.pos as usize;
            let frac = (self.pos - i as f64) as f32;
            out.push(filtered[i] * (1.0 - frac) + filtered[i + 1] * frac);
            self.pos += ratio;
        }
        // Carry over the unconsumed tail by shifting pos relative to next block.
        self.pos -= len as f64;
        // We dropped `len` samples but interpolation between the last sample of this
        // block and the first of the next is approximated by restarting at pos>=0.
        if self.pos < 0.0 {
            self.pos = 0.0;
        }
        out
    }
}

fn lowpass(cutoff: f32, taps: usize) -> Vec<f32> {
    let m = (taps - 1) as f32;
    let mut h: Vec<f32> = (0..taps)
        .map(|i| {
            let x = i as f32 - m / 2.0;
            let sinc = if x == 0.0 { 2.0 * cutoff } else { (2.0 * std::f32::consts::PI * cutoff * x).sin() / (std::f32::consts::PI * x) };
            let w = 0.54 - 0.46 * (2.0 * std::f32::consts::PI * i as f32 / m).cos(); // Hamming
            sinc * w
        })
        .collect();
    let sum: f32 = h.iter().sum();
    for v in &mut h {
        *v /= sum;
    }
    h
}

pub fn f32_to_i16(x: &[f32]) -> Vec<i16> {
    x.iter().map(|&v| (v.clamp(-1.0, 1.0) * 32767.0) as i16).collect()
}

pub fn downmix(interleaved: &[f32], channels: usize) -> Vec<f32> {
    if channels <= 1 {
        return interleaved.to_vec();
    }
    interleaved.chunks(channels).map(|c| c.iter().sum::<f32>() / channels as f32).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn downsamples_48k_to_16k_with_right_length() {
        let mut r = Resampler::new(48_000, 16_000);
        let input: Vec<f32> = (0..4800).map(|i| ((i as f32) * 0.05).sin()).collect();
        let out = r.process(&input);
        assert!((out.len() as i64 - 1600).abs() <= 2, "got {}", out.len());
    }
}
