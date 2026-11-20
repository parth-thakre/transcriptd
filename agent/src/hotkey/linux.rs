//! evdev backend: one reader thread per keyboard-like device, plus a rescan thread so
//! keyboards plugged in later are picked up.

use std::collections::HashSet;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use anyhow::{anyhow, Context, Result};
use crossbeam_channel::Sender;
use evdev::{Device, EventType, KeyCode};

use super::{ChordTracker, HotkeyEvent, HotkeyId, HotkeySpec};

pub fn start(spec: HotkeySpec, tx: Sender<HotkeyEvent>) -> Result<()> {
    let mut chords = vec![(HotkeyId::Ptt, spec.ptt)];
    if let Some(c) = spec.correction {
        chords.push((HotkeyId::Correction, c));
    }
    let tracker = Arc::new(Mutex::new(ChordTracker::new(chords)));
    let ignore = spec.ignore_device_name;

    // Fail fast if we cannot read any device at all (missing udev rule).
    let devs = keyboards(&ignore);
    if devs.is_empty() {
        return Err(anyhow!(
            "no readable keyboard under /dev/input. Install packaging/70-transcriptd.rules \
             (uaccess for /dev/input/event* and /dev/uinput) and run `sudo udevadm control --reload && sudo udevadm trigger`"
        ));
    }

    let open: Arc<Mutex<HashSet<String>>> = Arc::new(Mutex::new(HashSet::new()));
    for (path, dev) in devs {
        spawn_reader(path, dev, tracker.clone(), tx.clone(), open.clone());
    }
    // Hotplug: rescan every 3 s for new devices.
    thread::Builder::new().name("hotkey-rescan".into()).spawn(move || loop {
        thread::sleep(Duration::from_secs(3));
        for (path, dev) in keyboards(&ignore) {
            if !open.lock().unwrap().contains(&path) {
                spawn_reader(path, dev, tracker.clone(), tx.clone(), open.clone());
            }
        }
    })?;
    Ok(())
}

fn keyboards(ignore: &str) -> Vec<(String, Device)> {
    let mut out = Vec::new();
    let Ok(rd) = std::fs::read_dir("/dev/input") else { return out };
    for entry in rd.flatten() {
        let path = entry.path();
        let name = path.file_name().unwrap().to_string_lossy().to_string();
        if !name.starts_with("event") {
            continue;
        }
        let Ok(dev) = Device::open(&path) else { continue };
        if dev.name().map(|n| n == ignore).unwrap_or(false) {
            continue;
        }
        let Some(keys) = dev.supported_keys() else { continue };
        // Keyboard-ish: has letters or at least a space bar / function keys.
        if keys.contains(KeyCode::KEY_A) || keys.contains(KeyCode::KEY_SPACE) || keys.contains(KeyCode::KEY_F13) {
            out.push((path.to_string_lossy().to_string(), dev));
        }
    }
    out
}

fn spawn_reader(
    path: String,
    mut dev: Device,
    tracker: Arc<Mutex<ChordTracker>>,
    tx: Sender<HotkeyEvent>,
    open: Arc<Mutex<HashSet<String>>>,
) {
    open.lock().unwrap().insert(path.clone());
    let name = dev.name().unwrap_or("?").to_string();
    tracing::info!("hotkey: listening on {path} ({name})");
    let _ = thread::Builder::new().name(format!("hotkey-{path}")).spawn(move || {
        loop {
            let events = match dev.fetch_events() {
                Ok(ev) => ev.collect::<Vec<_>>(),
                Err(e) => {
                    tracing::info!("hotkey: {path} closed ({e})");
                    break;
                }
            };
            for ev in events {
                if ev.event_type() != EventType::KEY {
                    continue;
                }
                let key = KeyCode::new(ev.code());
                let pressed = match ev.value() {
                    0 => false,
                    1 => true,
                    _ => continue, // 2 = autorepeat
                };
                let name = key_name(key);
                let fired = tracker.lock().unwrap().feed(&name, pressed);
                for f in fired {
                    let _ = tx.send(f);
                }
            }
        }
        open.lock().unwrap().remove(&path);
    });
}

/// evdev KEY_* -> lowercase platform-neutral name ("KEY_LEFTCTRL" -> "leftctrl").
pub fn key_name(key: KeyCode) -> String {
    let s = format!("{key:?}");
    s.strip_prefix("KEY_").unwrap_or(&s).to_ascii_lowercase()
}

/// Reverse mapping for the injector.
pub fn key_from_name(name: &str) -> Result<KeyCode> {
    let upper = format!("KEY_{}", name.to_ascii_uppercase());
    // evdev has no FromStr; scan the plausible range once.
    for code in 0u16..0x300 {
        let k = KeyCode::new(code);
        if format!("{k:?}") == upper {
            return Ok(k);
        }
    }
    Err(anyhow!("unknown key name {name:?}")).context("key_from_name")
}
