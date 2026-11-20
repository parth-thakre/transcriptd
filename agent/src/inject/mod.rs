//! Put text into whatever has focus.
//!
//! Default path: insert at the caret through the accessibility layer (AT-SPI
//! EditableText on Linux, verified Unicode typing on Windows, see `readback`). When
//! that is unavailable for the focused control, put the text on the clipboard, send
//! the paste chord and restore the old clipboard. Direct typing is offered on Windows
//! (Unicode SendInput); on Linux, uinput can only send keycodes, which are layout
//! dependent, so we stay with the clipboard.

use std::thread;
use std::time::Duration;

use anyhow::Result;

use crate::config::{InjectConfig, InjectMethod};
use crate::readback::Readback;

#[cfg(target_os = "linux")]
pub mod linux;
#[cfg(windows)]
pub mod windows;

pub trait Injector: Send {
    fn name(&self) -> &'static str;
    /// Press+release a chord like "shift+insert" / "ctrl+c".
    fn send_chord(&mut self, chord: &str) -> Result<()>;
    /// Type unicode text directly, if supported.
    fn type_text(&mut self, _text: &str) -> Result<bool> {
        Ok(false)
    }
}

pub fn new_injector(virtual_device_name: &str) -> Result<Box<dyn Injector>> {
    #[cfg(target_os = "linux")]
    {
        Ok(Box::new(linux::UinputInjector::new(virtual_device_name)?))
    }
    #[cfg(windows)]
    {
        let _ = virtual_device_name;
        Ok(Box::new(windows::SendInputInjector))
    }
    #[cfg(not(any(target_os = "linux", windows)))]
    {
        let _ = virtual_device_name;
        Err(anyhow::anyhow!("no injector for this platform"))
    }
}

pub fn default_paste_chord() -> &'static str {
    if cfg!(windows) {
        "ctrl+v"
    } else {
        "shift+insert"
    }
}

pub fn clipboard_read() -> Option<String> {
    arboard::Clipboard::new().ok()?.get_text().ok()
}

pub fn clipboard_write(text: &str) -> Result<()> {
    arboard::Clipboard::new()?.set_text(text.to_string())?;
    Ok(())
}

/// What was on the clipboard before we borrowed it, in the richest form arboard can
/// put back: copied files, then text (with its HTML flavour), then an image.
enum SavedClipboard {
    Files(Vec<std::path::PathBuf>),
    Text { text: String, html: Option<String> },
    Image(arboard::ImageData<'static>),
    /// Empty, or a format we cannot read back.
    Nothing,
}

impl SavedClipboard {
    fn take() -> Self {
        let Ok(mut c) = arboard::Clipboard::new() else { return Self::Nothing };
        if let Ok(files) = c.get().file_list() {
            if !files.is_empty() {
                return Self::Files(files);
            }
        }
        if let Ok(text) = c.get().text() {
            let html = c.get().html().ok().filter(|h| !h.is_empty());
            return Self::Text { text, html };
        }
        match c.get().image() {
            Ok(img) => Self::Image(img),
            Err(_) => Self::Nothing,
        }
    }

    fn restore(self) {
        let Ok(mut c) = arboard::Clipboard::new() else { return };
        let _ = match self {
            Self::Files(files) => c.set().file_list(&files),
            Self::Text { text, html: Some(html) } => c.set().html(html, Some(text)),
            Self::Text { text, html: None } => c.set().text(text),
            Self::Image(img) => c.set().image(img),
            Self::Nothing => c.clear(),
        };
    }
}

/// Inject `text` using the configured method, without an accessibility backend
/// (`InjectMethod::Accessibility` therefore uses the clipboard).
#[allow(dead_code)]
pub fn inject(inj: &mut dyn Injector, cfg: &InjectConfig, text: &str) -> Result<&'static str> {
    inject_with(inj, cfg, text, None)
}

/// Inject `text` using the configured method. Returns the method actually used:
/// "accessibility", "type" or "clipboard".
///
/// With `InjectMethod::Accessibility` and a `readback` backend, the text is first
/// inserted at the caret through the accessibility layer. If the backend reports that
/// it cannot do it (`Ok(false)`) or fails, the clipboard path runs instead. A backend
/// only returns `Ok(false)` when it did not change the field, so this never inserts
/// the text twice.
pub fn inject_with(
    inj: &mut dyn Injector,
    cfg: &InjectConfig,
    text: &str,
    readback: Option<&mut dyn Readback>,
) -> Result<&'static str> {
    let mut text = text.to_string();
    if cfg.trailing_space && !text.ends_with(' ') {
        text.push(' ');
    }
    if cfg.method == InjectMethod::Accessibility {
        if let Some(rb) = readback {
            match rb.insert_at_caret(&text) {
                Ok(true) => return Ok("accessibility"),
                Ok(false) => tracing::debug!("{}: insert_at_caret unavailable, using clipboard", rb.name()),
                Err(e) => tracing::debug!("{}: insert_at_caret failed, using clipboard: {e:#}", rb.name()),
            }
        }
    }
    if cfg.method == InjectMethod::Type && inj.type_text(&text)? {
        return Ok("type");
    }
    let previous = if cfg.restore_clipboard { Some(SavedClipboard::take()) } else { None };
    clipboard_write(&text)?;
    // Give the clipboard manager / compositor a moment to see the new selection owner.
    thread::sleep(Duration::from_millis(40));
    let chord = cfg.paste_chord.clone().unwrap_or_else(|| default_paste_chord().to_string());
    inj.send_chord(&chord)?;
    if let Some(previous) = previous {
        let delay = cfg.restore_delay_ms;
        let ours = text.clone();
        thread::spawn(move || {
            thread::sleep(Duration::from_millis(delay));
            // Only restore if nobody else changed the clipboard in the meantime.
            if clipboard_read().as_deref() == Some(ours.as_str()) {
                previous.restore();
            }
        });
    }
    Ok("clipboard")
}

/// Copy the current selection via the copy chord and read it back. The previous
/// clipboard is restored afterwards.
pub fn read_selection(inj: &mut dyn Injector) -> Result<Option<String>> {
    let previous = SavedClipboard::take();
    let marker = format!("\u{200b}transcriptd-{}", std::process::id());
    clipboard_write(&marker)?;
    thread::sleep(Duration::from_millis(30));
    inj.send_chord("ctrl+c")?;
    let mut got = None;
    for _ in 0..20 {
        thread::sleep(Duration::from_millis(25));
        if let Some(t) = clipboard_read() {
            if t != marker {
                got = Some(t);
                break;
            }
        }
    }
    previous.restore();
    Ok(got)
}

/// Parse "ctrl+shift+v" into (modifier key names, main key name).
pub fn split_chord(chord: &str) -> (Vec<&'static str>, String) {
    let mut mods = Vec::new();
    let mut key = String::new();
    for p in chord.split('+').map(|s| s.trim().to_ascii_lowercase()) {
        match p.as_str() {
            "ctrl" | "control" => mods.push("leftctrl"),
            "shift" => mods.push("leftshift"),
            "alt" => mods.push("leftalt"),
            "super" | "meta" | "win" => mods.push("leftmeta"),
            k => key = k.to_string(),
        }
    }
    (mods, key)
}
