//! uinput virtual keyboard. Sits below the compositor, so it works on GNOME, KDE,
//! wlroots and X11 alike. Needs write access to /dev/uinput (udev rule).

use std::thread;
use std::time::Duration;

use anyhow::{Context, Result};
use evdev::uinput::VirtualDevice;
use evdev::{AttributeSet, EventType, InputEvent, KeyCode};

use super::{split_chord, Injector};
use crate::hotkey::linux::key_from_name;

pub struct UinputInjector {
    dev: VirtualDevice,
}

impl UinputInjector {
    pub fn new(name: &str) -> Result<Self> {
        let mut keys = AttributeSet::<KeyCode>::new();
        for code in 1u16..=0x1ff {
            keys.insert(KeyCode::new(code));
        }
        let dev = VirtualDevice::builder()
            .context("open /dev/uinput (install packaging/70-transcriptd.rules)")?
            .name(name)
            .with_keys(&keys)?
            .build()?;
        // The compositor needs a moment to pick up the new device before the first
        // event, otherwise the first chord is lost.
        thread::sleep(Duration::from_millis(200));
        Ok(Self { dev })
    }

    fn key(&mut self, k: KeyCode, down: bool) -> Result<()> {
        let ev = InputEvent::new(EventType::KEY.0, k.0, if down { 1 } else { 0 });
        self.dev.emit(&[ev])?;
        Ok(())
    }
}

impl Injector for UinputInjector {
    fn name(&self) -> &'static str {
        "uinput"
    }

    fn send_chord(&mut self, chord: &str) -> Result<()> {
        let (mods, key) = split_chord(chord);
        let mod_keys: Vec<KeyCode> = mods.iter().map(|m| key_from_name(m)).collect::<Result<_>>()?;
        let main = key_from_name(&key)?;
        for m in &mod_keys {
            self.key(*m, true)?;
        }
        thread::sleep(Duration::from_millis(10));
        self.key(main, true)?;
        thread::sleep(Duration::from_millis(10));
        self.key(main, false)?;
        for m in mod_keys.iter().rev() {
            self.key(*m, false)?;
        }
        Ok(())
    }
}
