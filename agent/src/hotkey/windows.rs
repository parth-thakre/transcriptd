//! WH_KEYBOARD_LL backend. Runs a message loop on its own thread; no permissions
//! needed and it sees keys even over fullscreen games (unlike RegisterHotKey).

use std::sync::Mutex;
use std::thread;

use anyhow::Result;
use crossbeam_channel::Sender;
use windows::Win32::Foundation::{LPARAM, LRESULT, WPARAM};
use windows::Win32::UI::Input::KeyboardAndMouse::*;
use windows::Win32::UI::WindowsAndMessaging::*;

use super::{ChordTracker, HotkeyEvent, HotkeyId, HotkeySpec};

static STATE: Mutex<Option<(ChordTracker, Sender<HotkeyEvent>)>> = Mutex::new(None);

pub fn start(spec: HotkeySpec, tx: Sender<HotkeyEvent>) -> Result<()> {
    let mut chords = vec![(HotkeyId::Ptt, spec.ptt)];
    if let Some(c) = spec.correction {
        chords.push((HotkeyId::Correction, c));
    }
    *STATE.lock().unwrap() = Some((ChordTracker::new(chords), tx));
    thread::Builder::new().name("hotkey-hook".into()).spawn(|| unsafe {
        let hook = SetWindowsHookExW(WH_KEYBOARD_LL, Some(hook_proc), None, 0).expect("SetWindowsHookExW");
        let mut msg = MSG::default();
        while GetMessageW(&mut msg, None, 0, 0).as_bool() {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
        let _ = UnhookWindowsHookEx(hook);
    })?;
    Ok(())
}

unsafe extern "system" fn hook_proc(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    if code >= 0 {
        let kb = &*(lparam.0 as *const KBDLLHOOKSTRUCT);
        // Ignore events we injected ourselves (SendInput sets LLKHF_INJECTED).
        if kb.flags.0 & LLKHF_INJECTED.0 == 0 {
            let pressed = matches!(wparam.0 as u32, WM_KEYDOWN | WM_SYSKEYDOWN);
            let name = vk_name(VIRTUAL_KEY(kb.vkCode as u16), kb.flags.0 & LLKHF_EXTENDED.0 != 0);
            if let Some(name) = name {
                if let Some((tracker, tx)) = STATE.lock().unwrap().as_mut() {
                    for ev in tracker.feed(&name, pressed) {
                        let _ = tx.send(ev);
                    }
                }
            }
        }
    }
    CallNextHookEx(None, code, wparam, lparam)
}

/// Virtual key -> the same lowercase names the Linux backend produces.
pub fn vk_name(vk: VIRTUAL_KEY, extended: bool) -> Option<String> {
    let s = match vk {
        VK_LCONTROL => "leftctrl",
        VK_RCONTROL => "rightctrl",
        VK_CONTROL => if extended { "rightctrl" } else { "leftctrl" },
        VK_LMENU => "leftalt",
        VK_RMENU => "rightalt",
        VK_MENU => if extended { "rightalt" } else { "leftalt" },
        VK_LSHIFT => "leftshift",
        VK_RSHIFT => "rightshift",
        VK_SHIFT => "leftshift",
        VK_LWIN => "leftmeta",
        VK_RWIN => "rightmeta",
        VK_SPACE => "space",
        VK_RETURN => "enter",
        VK_TAB => "tab",
        VK_ESCAPE => "esc",
        VK_BACK => "backspace",
        VK_INSERT => "insert",
        VK_DELETE => "delete",
        VK_HOME => "home",
        VK_END => "end",
        VK_PRIOR => "pageup",
        VK_NEXT => "pagedown",
        VK_SCROLL => "scrolllock",
        VK_PAUSE => "pause",
        VK_CAPITAL => "capslock",
        VK_LEFT => "left",
        VK_RIGHT => "right",
        VK_UP => "up",
        VK_DOWN => "down",
        VK_OEM_3 => "grave",
        VK_OEM_MINUS => "minus",
        VK_OEM_PLUS => "equal",
        VK_OEM_4 => "leftbrace",
        VK_OEM_6 => "rightbrace",
        VK_OEM_5 => "backslash",
        VK_OEM_1 => "semicolon",
        VK_OEM_7 => "apostrophe",
        VK_OEM_COMMA => "comma",
        VK_OEM_PERIOD => "dot",
        VK_OEM_2 => "slash",
        _ => {
            let v = vk.0;
            if (0x30..=0x39).contains(&v) {
                return Some(((v - 0x30) as u8 + b'0').to_string()); // "0".."9"
            }
            if (0x41..=0x5A).contains(&v) {
                return Some(((v - 0x41) as u8 + b'a').to_string()); // "a".."z"
            }
            if (VK_F1.0..=VK_F24.0).contains(&v) {
                return Some(format!("f{}", v - VK_F1.0 + 1));
            }
            return None;
        }
    };
    Some(s.to_string())
}
