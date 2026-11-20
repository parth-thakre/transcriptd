//! Read the focused text field back after a paste, so corrections the user makes in the
//! target application can be learned without any review UI.
//!
//! Windows: UI Automation (TextPattern / ValuePattern on the focused element).
//! Linux: AT-SPI2 over D-Bus (Text interface on the focused accessible). Works on X11
//! and Wayland for GTK, Qt, Chromium/Electron and Firefox; not for apps that do not
//! expose their text.
//!
//! Platform code lives in `windows.rs` / `linux.rs` and implements [`Readback`].

use anyhow::Result;

pub mod diff;
#[cfg(target_os = "linux")]
pub mod linux;
#[cfg(windows)]
pub mod windows;

/// What the focused editable control contains right now.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FieldSnapshot {
    /// Process/application name if known (e.g. "firefox", "Code.exe").
    pub app: String,
    /// Who owns the control: process id (Windows) or accessibility bus name (Linux).
    /// Lets an app switch be noticed even when the control itself has no id.
    pub owner: String,
    /// Stable identifier of the control while it exists (UIA "{pid}:{RuntimeId}" /
    /// AT-SPI bus name + path). Used to notice that focus moved to a different field.
    /// Empty when the platform cannot name the control (UIA without a RuntimeId).
    pub control_id: String,
    /// Full text of the control (may be large; implementations should cap at ~200 kB).
    pub text: String,
    /// Caret offset in chars, if the platform exposes it.
    pub caret: Option<usize>,
}

pub trait Readback: Send {
    fn name(&self) -> &'static str;
    /// Text of the focused editable control, or `Ok(None)` when the focused element is
    /// not a text control or does not expose text. Must return quickly (< 100 ms typical);
    /// never block on the target application for more than ~1 s.
    fn focused_text(&mut self) -> Result<Option<FieldSnapshot>>;

    /// Insert `text` at the caret of the focused editable control through the
    /// accessibility layer (AT-SPI EditableText / UIA). Returns Ok(false) when the
    /// platform or control cannot do it, so the caller falls back to clipboard/keys.
    fn insert_at_caret(&mut self, _text: &str) -> Result<bool> {
        Ok(false)
    }
}

impl FieldSnapshot {
    pub fn focus_id(&self) -> FocusId {
        FocusId { owner: self.owner.clone(), control: self.control_id.clone() }
    }
}

/// Identity of the focused field, as far as the platform can tell. Empty strings are
/// unknown parts.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct FocusId {
    pub owner: String,
    pub control: String,
}

/// Whether the field that had focus when the take started still has it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FocusCheck {
    /// No field could be identified when the take started, so there is nothing to
    /// compare against (apps without accessibility, no backend). Injection proceeds.
    Unverifiable,
    /// The same control has focus.
    Same,
    /// Another control has focus, or the one from the start of the take can no longer
    /// be seen. Injecting would put the text somewhere the user did not dictate into.
    Moved,
}

/// Compare the focused field at hotkey press with the one right before injecting.
///
/// A known owner that changed is `Moved` (another app). With the same owner, the
/// controls decide: equal ids are `Same`, different ones `Moved`, and when the press
/// had no control id (UIA without a RuntimeId) another field in the same app cannot
/// be ruled out or confirmed, so it is `Unverifiable`. Once something was known at
/// press, seeing nothing now is `Moved`.
pub fn focus_check(at_press: Option<&FocusId>, now: Option<&FocusId>) -> FocusCheck {
    let Some(a) = at_press.filter(|a| !a.owner.is_empty() || !a.control.is_empty()) else {
        return FocusCheck::Unverifiable;
    };
    let Some(b) = now.filter(|b| !b.owner.is_empty() || !b.control.is_empty()) else {
        return FocusCheck::Moved;
    };
    if !a.owner.is_empty() && a.owner != b.owner {
        return FocusCheck::Moved;
    }
    match (a.control.as_str(), b.control.as_str()) {
        ("", _) => FocusCheck::Unverifiable,
        (x, y) if x == y => FocusCheck::Same,
        _ => FocusCheck::Moved,
    }
}

/// Create the platform backend, or `None` when unsupported here.
pub fn new_readback() -> Option<Box<dyn Readback>> {
    #[cfg(windows)]
    {
        match windows::UiaReadback::new() {
            Ok(r) => return Some(Box::new(r)),
            Err(e) => {
                tracing::warn!("readback unavailable: {e:#}");
                return None;
            }
        }
    }
    #[cfg(target_os = "linux")]
    {
        match linux::AtspiReadback::new() {
            Ok(r) => return Some(Box::new(r)),
            Err(e) => {
                tracing::warn!("readback unavailable: {e:#}");
                return None;
            }
        }
    }
    #[allow(unreachable_code)]
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    fn id(owner: &str, control: &str) -> FocusId {
        FocusId { owner: owner.into(), control: control.into() }
    }

    #[test]
    fn focus_check_cases() {
        let field = id(":1.42", ":1.42/org/a11y/atspi/accessible/17");
        let check = |a: Option<&FocusId>, b: Option<&FocusId>| focus_check(a, b);
        assert_eq!(check(Some(&field), Some(&field)), FocusCheck::Same);
        assert_eq!(check(Some(&field), Some(&id(":1.42", ":1.42/org/a11y/atspi/accessible/18"))), FocusCheck::Moved);
        assert_eq!(check(Some(&field), Some(&id(":1.77", ":1.77/org/a11y/atspi/accessible/17"))), FocusCheck::Moved);
        // Was verifiable at press, cannot be seen now: do not guess.
        assert_eq!(check(Some(&field), None), FocusCheck::Moved);
        assert_eq!(check(Some(&field), Some(&id("", ""))), FocusCheck::Moved);
        // Nothing known at press: behave as before (inject).
        assert_eq!(check(None, None), FocusCheck::Unverifiable);
        assert_eq!(check(None, Some(&field)), FocusCheck::Unverifiable);
        assert_eq!(check(Some(&id("", "")), Some(&field)), FocusCheck::Unverifiable);
        // Windows: owner is the pid, control "{pid}:{runtime id}".
        let edit = id("1234", "1234:42.7.1");
        assert_eq!(check(Some(&edit), Some(&edit)), FocusCheck::Same);
        assert_eq!(check(Some(&edit), Some(&id("1234", "1234:42.7.2"))), FocusCheck::Moved);
        assert_eq!(check(Some(&edit), Some(&id("5678", "5678:42.7.1"))), FocusCheck::Moved);
        // Known field at press, now a control without a runtime id: not provably the same.
        assert_eq!(check(Some(&edit), Some(&id("1234", ""))), FocusCheck::Moved);
        // No runtime id at press: the same app may be any of its fields, so the field is
        // unverifiable; another app is still a move.
        let anon = id("1234", "");
        assert_eq!(check(Some(&anon), Some(&anon)), FocusCheck::Unverifiable);
        assert_eq!(check(Some(&anon), Some(&edit)), FocusCheck::Unverifiable);
        assert_eq!(check(Some(&anon), Some(&id("5678", ""))), FocusCheck::Moved);
        assert_eq!(check(Some(&anon), Some(&id("5678", "5678:42.7.1"))), FocusCheck::Moved);
        assert_eq!(check(Some(&anon), None), FocusCheck::Moved);
    }
}
