//! SendInput injector: paste chords via virtual keys, direct typing via KEYEVENTF_UNICODE.

use std::thread;
use std::time::Duration;

use anyhow::{anyhow, Result};
use windows::Win32::UI::Input::KeyboardAndMouse::*;

use super::{split_chord, Injector};

pub struct SendInputInjector;

fn vk(name: &str) -> Result<VIRTUAL_KEY> {
    Ok(match name {
        "leftctrl" | "ctrl" => VK_LCONTROL,
        "leftshift" | "shift" => VK_LSHIFT,
        "leftalt" | "alt" => VK_LMENU,
        "leftmeta" | "super" => VK_LWIN,
        "insert" => VK_INSERT,
        "v" => VIRTUAL_KEY(b'V' as u16),
        "c" => VIRTUAL_KEY(b'C' as u16),
        other if other.len() == 1 && other.chars().all(|c| c.is_ascii_alphanumeric()) => {
            VIRTUAL_KEY(other.to_ascii_uppercase().as_bytes()[0] as u16)
        }
        other => return Err(anyhow!("unsupported key {other:?}")),
    })
}

fn key_input(k: VIRTUAL_KEY, up: bool) -> INPUT {
    INPUT {
        r#type: INPUT_KEYBOARD,
        Anonymous: INPUT_0 {
            ki: KEYBDINPUT { wVk: k, wScan: 0, dwFlags: if up { KEYEVENTF_KEYUP } else { KEYBD_EVENT_FLAGS(0) }, time: 0, dwExtraInfo: 0 },
        },
    }
}

fn unicode_input(unit: u16, up: bool) -> INPUT {
    INPUT {
        r#type: INPUT_KEYBOARD,
        Anonymous: INPUT_0 {
            ki: KEYBDINPUT {
                wVk: VIRTUAL_KEY(0),
                wScan: unit,
                dwFlags: if up { KEYEVENTF_UNICODE | KEYEVENTF_KEYUP } else { KEYEVENTF_UNICODE },
                time: 0,
                dwExtraInfo: 0,
            },
        },
    }
}

fn send(inputs: &[INPUT]) -> Result<()> {
    let n = unsafe { SendInput(inputs, std::mem::size_of::<INPUT>() as i32) };
    if n as usize != inputs.len() {
        return Err(anyhow!("SendInput sent {n}/{} events (elevated window?)", inputs.len()));
    }
    Ok(())
}

/// Release any modifier the user is physically holding so it does not combine with
/// what we send (e.g. the PTT chord's Ctrl turning our 'v' into Ctrl+V twice).
fn release_user_modifiers() {
    let mut ev = Vec::new();
    for k in [VK_LCONTROL, VK_RCONTROL, VK_LSHIFT, VK_RSHIFT, VK_LMENU, VK_RMENU, VK_LWIN, VK_RWIN] {
        if unsafe { GetAsyncKeyState(k.0 as i32) } as u16 & 0x8000 != 0 {
            ev.push(key_input(k, true));
        }
    }
    if !ev.is_empty() {
        let _ = send(&ev);
        thread::sleep(Duration::from_millis(10));
    }
}

impl Injector for SendInputInjector {
    fn name(&self) -> &'static str {
        "sendinput"
    }

    fn send_chord(&mut self, chord: &str) -> Result<()> {
        release_user_modifiers();
        let (mods, key) = split_chord(chord);
        let mut ev = Vec::new();
        for m in &mods {
            ev.push(key_input(vk(m)?, false));
        }
        let main = vk(&key)?;
        ev.push(key_input(main, false));
        ev.push(key_input(main, true));
        for m in mods.iter().rev() {
            ev.push(key_input(vk(m)?, true));
        }
        send(&ev)
    }

    fn type_text(&mut self, text: &str) -> Result<bool> {
        type_unicode(text)?;
        Ok(true)
    }
}

/// Type `text` into the focused window as KEYEVENTF_UNICODE key events (layout
/// independent). Physically held modifiers are released first. On error, the chunks
/// before the failing one have already been delivered.
pub fn type_unicode(text: &str) -> Result<()> {
    release_user_modifiers();
    let mut ev = Vec::with_capacity(text.len() * 2);
    for unit in text.encode_utf16() {
        ev.push(unicode_input(unit, false));
        ev.push(unicode_input(unit, true));
    }
    // Chunk so slow apps do not drop events.
    for chunk in ev.chunks(64) {
        send(chunk)?;
        thread::sleep(Duration::from_millis(2));
    }
    Ok(())
}
