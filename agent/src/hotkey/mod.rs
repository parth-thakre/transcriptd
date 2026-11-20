//! Global hotkey listening. Emits Down/Up for the configured chord from any app.
//!
//! Linux: evdev on /dev/input/event* (works on X11, Wayland, any compositor; needs
//! read access, see packaging/70-transcriptd.rules). Windows: WH_KEYBOARD_LL hook.

use std::collections::HashSet;

use anyhow::{anyhow, Result};
use crossbeam_channel::Sender;

#[cfg(target_os = "linux")]
pub mod linux;
#[cfg(windows)]
pub mod windows;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HotkeyEvent {
    Down(HotkeyId),
    Up(HotkeyId),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum HotkeyId {
    Ptt,
    Correction,
}

/// A parsed chord: zero or more modifiers plus one main key, all as platform-neutral names.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Chord {
    pub modifiers: HashSet<Modifier>,
    pub key: String, // lowercase key name: "space", "a", "f13", "rightalt", "scrolllock"
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Modifier {
    Ctrl,
    Alt,
    Shift,
    Super,
}

impl Chord {
    pub fn parse(spec: &str) -> Result<Self> {
        let mut modifiers = HashSet::new();
        let mut key = None;
        for part in spec.split('+').map(|p| p.trim().to_ascii_lowercase()) {
            match part.as_str() {
                "ctrl" | "control" => {
                    modifiers.insert(Modifier::Ctrl);
                }
                "alt" | "option" => {
                    modifiers.insert(Modifier::Alt);
                }
                "shift" => {
                    modifiers.insert(Modifier::Shift);
                }
                "super" | "meta" | "win" | "cmd" => {
                    modifiers.insert(Modifier::Super);
                }
                "" => {}
                k => {
                    if key.replace(k.to_string()).is_some() {
                        return Err(anyhow!("chord {spec:?} has more than one non-modifier key"));
                    }
                }
            }
        }
        let key = key.ok_or_else(|| anyhow!("chord {spec:?} has no main key"))?;
        Ok(Self { modifiers, key })
    }

    /// True when `key` is a modifier key name itself (e.g. hotkey = "rightalt").
    #[allow(dead_code)]
    pub fn main_is_modifier(&self) -> bool {
        matches!(
            self.key.as_str(),
            "leftctrl" | "rightctrl" | "leftalt" | "rightalt" | "leftshift" | "rightshift" | "leftmeta" | "rightmeta"
        )
    }
}

/// Shared chord-state tracker used by both platform backends. Feed it key names with
/// pressed/released and it tells you when a registered chord fires or releases.
pub struct ChordTracker {
    chords: Vec<(HotkeyId, Chord)>,
    held: HashSet<String>,
    active: HashSet<HotkeyId>,
}

impl ChordTracker {
    pub fn new(chords: Vec<(HotkeyId, Chord)>) -> Self {
        Self { chords, held: HashSet::new(), active: HashSet::new() }
    }

    fn modifier_held(&self, m: Modifier) -> bool {
        let (a, b) = match m {
            Modifier::Ctrl => ("leftctrl", "rightctrl"),
            Modifier::Alt => ("leftalt", "rightalt"),
            Modifier::Shift => ("leftshift", "rightshift"),
            Modifier::Super => ("leftmeta", "rightmeta"),
        };
        self.held.contains(a) || self.held.contains(b)
    }

    /// Returns events to emit. `key` is a lowercase platform-neutral key name.
    pub fn feed(&mut self, key: &str, pressed: bool) -> Vec<HotkeyEvent> {
        let mut out = Vec::new();
        if pressed {
            if !self.held.insert(key.to_string()) {
                return out; // auto-repeat
            }
        } else if !self.held.remove(key) {
            return out;
        }
        for (id, chord) in &self.chords {
            let satisfied = self.held.contains(&chord.key) && chord.modifiers.iter().all(|m| self.modifier_held(*m));
            if satisfied && !self.active.contains(id) {
                self.active.insert(*id);
                out.push(HotkeyEvent::Down(*id));
            } else if !satisfied && self.active.contains(id) {
                self.active.remove(id);
                out.push(HotkeyEvent::Up(*id));
            }
        }
        out
    }
}

pub struct HotkeySpec {
    pub ptt: Chord,
    pub correction: Option<Chord>,
    /// Name of our own uinput device so its events are ignored (Linux).
    pub ignore_device_name: String,
}

/// Start the platform listener on a background thread. Events arrive on `tx`.
/// Returns a human-readable backend name.
pub fn start(spec: HotkeySpec, tx: Sender<HotkeyEvent>) -> Result<&'static str> {
    #[cfg(target_os = "linux")]
    {
        linux::start(spec, tx)?;
        Ok("evdev")
    }
    #[cfg(windows)]
    {
        windows::start(spec, tx)?;
        Ok("win32-ll-hook")
    }
    #[cfg(not(any(target_os = "linux", windows)))]
    {
        let _ = (spec, tx);
        Err(anyhow!("no hotkey backend for this platform"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn chord_fires_and_releases() {
        let c = Chord::parse("ctrl+alt+space").unwrap();
        let mut t = ChordTracker::new(vec![(HotkeyId::Ptt, c)]);
        assert!(t.feed("leftctrl", true).is_empty());
        assert!(t.feed("leftalt", true).is_empty());
        assert_eq!(t.feed("space", true), vec![HotkeyEvent::Down(HotkeyId::Ptt)]);
        assert!(t.feed("space", true).is_empty()); // repeat
        assert_eq!(t.feed("leftctrl", false), vec![HotkeyEvent::Up(HotkeyId::Ptt)]);
        assert!(t.feed("space", false).is_empty());
    }

    #[test]
    fn single_modifier_key_as_hotkey() {
        let c = Chord::parse("rightalt").unwrap();
        assert!(c.main_is_modifier());
        let mut t = ChordTracker::new(vec![(HotkeyId::Ptt, c)]);
        assert_eq!(t.feed("rightalt", true), vec![HotkeyEvent::Down(HotkeyId::Ptt)]);
        assert_eq!(t.feed("rightalt", false), vec![HotkeyEvent::Up(HotkeyId::Ptt)]);
    }
}
