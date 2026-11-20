//! UI Automation read-back: text of the focused editable control via TextPattern
//! (preferred) or ValuePattern.
//!
//! COM is initialised apartment-threaded on the thread that calls `new()`. The
//! CUIAutomation object is registered ThreadingModel=Both, so if `focused_text()` is
//! ever called from another thread we initialise COM there for the duration of the call.
//! Connection and transaction timeouts (IUIAutomation2, Windows 8+) bound how long a
//! hung target application can block us.
//!
//! UIA has no general "insert at caret", so `insert_at_caret` types the text with
//! Unicode SendInput into a control it can read, then reads it back to verify.

use std::thread::{self, ThreadId};
use std::time::Duration;

use anyhow::{anyhow, Result};
use windows::core::{Interface, BSTR, HRESULT, PWSTR};
use windows::Win32::Foundation::{CloseHandle, RPC_E_CHANGED_MODE};
use windows::Win32::System::Com::{
    CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_INPROC_SERVER, COINIT_APARTMENTTHREADED, SAFEARRAY,
};
use windows::Win32::System::Ole::{
    SafeArrayDestroy, SafeArrayGetDim, SafeArrayGetElement, SafeArrayGetLBound, SafeArrayGetUBound,
};
use windows::Win32::System::Threading::{
    OpenProcess, QueryFullProcessImageNameW, PROCESS_NAME_WIN32, PROCESS_QUERY_LIMITED_INFORMATION,
};
use windows::Win32::UI::Accessibility::{
    CUIAutomation, IUIAutomation, IUIAutomation2, IUIAutomationElement, IUIAutomationTextPattern,
    IUIAutomationTextRange, IUIAutomationValuePattern, TextPatternRangeEndpoint_End,
    TextPatternRangeEndpoint_Start, UIA_ComboBoxControlTypeId, UIA_DocumentControlTypeId, UIA_EditControlTypeId,
    UIA_TextPatternId, UIA_ValuePatternId,
};

use super::{FieldSnapshot, Readback};

/// Cap on text we read, in UTF-16 units (GetText maxLength) / chars (ValuePattern).
const MAX_TEXT: i32 = 200_000;
/// Upper bound for a single cross-process UIA call, in ms.
const UIA_TIMEOUT_MS: u32 = 1_000;
/// SendInput only queues keys; poll this often, this many times, for them to land.
const VERIFY_POLL: Duration = Duration::from_millis(40);
const VERIFY_ATTEMPTS: u32 = 10;

/// Balances a successful `CoInitializeEx` with `CoUninitialize` on drop.
struct ComInit {
    owned: bool,
}

impl ComInit {
    /// Initialise COM (STA) on the current thread. An existing MTA initialisation
    /// (RPC_E_CHANGED_MODE) is fine: COM is usable, we just must not uninitialise it.
    fn new() -> Result<Self> {
        let hr: HRESULT = unsafe { CoInitializeEx(None, COINIT_APARTMENTTHREADED) };
        if hr == RPC_E_CHANGED_MODE {
            return Ok(Self { owned: false });
        }
        // S_OK or S_FALSE (already initialised as STA): both must be balanced.
        hr.ok().map_err(|e| anyhow!("CoInitializeEx: {e}"))?;
        Ok(Self { owned: true })
    }
}

impl Drop for ComInit {
    fn drop(&mut self) {
        if self.owned {
            unsafe { CoUninitialize() };
        }
    }
}

pub struct UiaReadback {
    // Field order matters: `automation` must be released before `_com` uninitialises.
    automation: IUIAutomation,
    thread: ThreadId,
    _com: ComInit,
}

// SAFETY: CUIAutomation is a free-threaded (ThreadingModel=Both) object, and the agent
// only ever uses this backend from one dedicated blocking thread. Calls from any other
// thread initialise COM for themselves first (see `focused_text`).
unsafe impl Send for UiaReadback {}

impl UiaReadback {
    pub fn new() -> Result<Self> {
        let com = ComInit::new()?;
        let automation: IUIAutomation = unsafe { CoCreateInstance(&CUIAutomation, None, CLSCTX_INPROC_SERVER) }
            .map_err(|e| anyhow!("CoCreateInstance(CUIAutomation): {e}"))?;
        // Best effort: IUIAutomation2 is Windows 8+.
        if let Ok(a2) = automation.cast::<IUIAutomation2>() {
            unsafe {
                let _ = a2.SetConnectionTimeout(UIA_TIMEOUT_MS);
                let _ = a2.SetTransactionTimeout(UIA_TIMEOUT_MS);
            }
        }
        Ok(Self { automation, thread: thread::current().id(), _com: com })
    }

    fn read_focused(&self) -> Result<Option<FieldSnapshot>> {
        let element = match unsafe { self.automation.GetFocusedElement() } {
            Ok(e) => e,
            Err(_) => return Ok(None),
        };
        // Disabled controls cannot hold user edits.
        if let Ok(enabled) = unsafe { element.CurrentIsEnabled() } {
            if !enabled.as_bool() {
                return Ok(None);
            }
        }
        let value_pattern: Option<IUIAutomationValuePattern> =
            unsafe { element.GetCurrentPatternAs::<IUIAutomationValuePattern>(UIA_ValuePatternId) }.ok();
        let read_only = value_pattern
            .as_ref()
            .and_then(|vp| unsafe { vp.CurrentIsReadOnly() }.ok())
            .map(|b| b.as_bool())
            .unwrap_or(false);
        if read_only {
            return Ok(None);
        }

        let (text, caret) = if let Some(r) = text_pattern_text(&element) {
            r
        } else if let Some(vp) = value_pattern.as_ref() {
            let ct = match unsafe { element.CurrentControlType() } {
                Ok(ct) => ct,
                Err(_) => return Ok(None),
            };
            if ct != UIA_EditControlTypeId && ct != UIA_DocumentControlTypeId && ct != UIA_ComboBoxControlTypeId {
                return Ok(None);
            }
            match unsafe { vp.CurrentValue() } {
                Ok(v) => (truncate_chars(bstr_to_string(&v), MAX_TEXT as usize), None),
                Err(_) => return Ok(None),
            }
        } else {
            return Ok(None);
        };

        let pid = unsafe { element.CurrentProcessId() }.unwrap_or(0);
        // Without a RuntimeId the control cannot be told apart from others in the same
        // process: leave its id empty so the focus check only compares the process.
        let control_id = runtime_id(&element).filter(|r| !r.is_empty()).map(|r| format!("{pid}:{r}")).unwrap_or_default();
        let owner = if pid > 0 { pid.to_string() } else { String::new() };
        let app = if pid > 0 { process_name(pid as u32) } else { String::new() };
        Ok(Some(FieldSnapshot { app, owner, control_id, text, caret }))
    }
}

impl Readback for UiaReadback {
    fn name(&self) -> &'static str {
        "uia"
    }

    fn focused_text(&mut self) -> Result<Option<FieldSnapshot>> {
        // Normally a no-op; keeps COM valid if the caller ever moves threads.
        let _guard = if thread::current().id() != self.thread { Some(ComInit::new()?) } else { None };
        self.read_focused()
    }

    /// Type `text` into the focused control and verify it by reading the control back.
    /// Ok(false) only when nothing was typed (the caller then pastes instead); once
    /// keys went out, Ok(true) even if verification fails, so the text is not doubled.
    fn insert_at_caret(&mut self, text: &str) -> Result<bool> {
        let _guard = if thread::current().id() != self.thread { Some(ComInit::new()?) } else { None };
        if text.is_empty() {
            return Ok(false);
        }
        // Only type into a control we can identify and read, so the result can be checked.
        let before = match self.read_focused() {
            Ok(Some(snap)) if !snap.control_id.is_empty() => snap,
            Ok(_) => {
                tracing::debug!("uia insert: focused element is not a readable text control");
                return Ok(false);
            }
            Err(e) => {
                tracing::debug!("uia insert: read focused control: {e:#}");
                return Ok(false);
            }
        };
        let typed = crate::inject::windows::type_unicode(text);
        // After a SendInput failure only a few polls: usually nothing was delivered.
        let attempts = if typed.is_ok() { VERIFY_ATTEMPTS } else { 3 };
        let mut after: Option<FieldSnapshot> = None;
        let mut verified = false;
        for _ in 0..attempts {
            thread::sleep(VERIFY_POLL);
            if let Ok(Some(snap)) = self.read_focused() {
                if snap.control_id == before.control_id {
                    verified = typed_text_landed(&before.text, &snap.text, text);
                    after = Some(snap);
                    if verified {
                        break;
                    }
                }
            }
        }
        match typed {
            Ok(()) => {
                if !verified {
                    tracing::warn!(
                        "uia insert: typed {} chars into {} ({}) but could not verify them in the control",
                        text.chars().count(), before.control_id, before.app
                    );
                }
                Ok(true)
            }
            Err(e) => {
                // Chunks before the failing SendInput call were delivered.
                let changed = verified || after.as_ref().is_some_and(|a| a.text != before.text);
                if changed {
                    tracing::warn!("uia insert: SendInput failed part-way ({e:#}); the control changed, not pasting again");
                    Ok(true)
                } else {
                    tracing::debug!("uia insert: SendInput failed and the control is unchanged: {e:#}");
                    Ok(false)
                }
            }
        }
    }
}

fn normalize_ws(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Whether `typed` shows up in the control: its whitespace-normalized form is in the
/// new text and was not in the old one, or the control grew by at least half its length.
fn typed_text_landed(before: &str, after: &str, typed: &str) -> bool {
    let needle = normalize_ws(typed);
    let appeared = !needle.is_empty() && normalize_ws(after).contains(&needle) && !normalize_ws(before).contains(&needle);
    appeared || after.chars().count() >= before.chars().count() + typed.chars().count() / 2
}

/// Text and caret via TextPattern, or None when the element has no usable TextPattern.
fn text_pattern_text(element: &IUIAutomationElement) -> Option<(String, Option<usize>)> {
    let tp: IUIAutomationTextPattern =
        unsafe { element.GetCurrentPatternAs::<IUIAutomationTextPattern>(UIA_TextPatternId) }.ok()?;
    let doc: IUIAutomationTextRange = unsafe { tp.DocumentRange() }.ok()?;
    let text = bstr_to_string(&unsafe { doc.GetText(MAX_TEXT) }.ok()?);
    let caret = caret_offset(&tp, &doc, &text);
    Some((text, caret))
}

/// Chars between the document start and the start of the first selection range.
/// Anything unexpected yields None rather than a guess.
fn caret_offset(tp: &IUIAutomationTextPattern, doc: &IUIAutomationTextRange, text: &str) -> Option<usize> {
    unsafe {
        let sel = tp.GetSelection().ok()?;
        if sel.Length().ok()? < 1 {
            return None;
        }
        let first: IUIAutomationTextRange = sel.GetElement(0).ok()?;
        // Selection must not start before the document does.
        if doc.CompareEndpoints(TextPatternRangeEndpoint_Start, &first, TextPatternRangeEndpoint_Start).ok()? > 0 {
            return None;
        }
        let prefix: IUIAutomationTextRange = doc.Clone().ok()?;
        prefix.MoveEndpointByRange(TextPatternRangeEndpoint_End, &first, TextPatternRangeEndpoint_Start).ok()?;
        let before = prefix.GetText(MAX_TEXT).ok()?;
        if before.len() >= MAX_TEXT as usize {
            return None; // truncated: offset unknown
        }
        let n = bstr_to_string(&before).chars().count();
        (n <= text.chars().count()).then_some(n)
    }
}

fn bstr_to_string(b: &BSTR) -> String {
    let wide: &[u16] = b;
    String::from_utf16_lossy(wide)
}

fn truncate_chars(mut s: String, max: usize) -> String {
    if let Some((idx, _)) = s.char_indices().nth(max) {
        s.truncate(idx);
    }
    s
}

/// RuntimeId as "a.b.c.d".
fn runtime_id(element: &IUIAutomationElement) -> Option<String> {
    unsafe {
        let sa: *mut SAFEARRAY = element.GetRuntimeId().ok()?;
        if sa.is_null() {
            return None;
        }
        let out = read_i32_vector(sa);
        let _ = SafeArrayDestroy(sa);
        out.map(|v| v.iter().map(|n| n.to_string()).collect::<Vec<_>>().join("."))
    }
}

unsafe fn read_i32_vector(sa: *const SAFEARRAY) -> Option<Vec<i32>> {
    unsafe {
        if SafeArrayGetDim(sa) != 1 {
            return None;
        }
        let lo = SafeArrayGetLBound(sa, 1).ok()?;
        let hi = SafeArrayGetUBound(sa, 1).ok()?;
        let mut v = Vec::with_capacity((hi - lo + 1).max(0) as usize);
        for i in lo..=hi {
            let mut n: i32 = 0;
            SafeArrayGetElement(sa, &i, &mut n as *mut i32 as *mut core::ffi::c_void).ok()?;
            v.push(n);
        }
        Some(v)
    }
}

/// Basename of the process image, or "" when it cannot be queried (e.g. elevated target).
fn process_name(pid: u32) -> String {
    unsafe {
        let Ok(h) = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid) else {
            return String::new();
        };
        let mut buf = [0u16; 1024];
        let mut size = buf.len() as u32;
        let ok = QueryFullProcessImageNameW(h, PROCESS_NAME_WIN32, PWSTR(buf.as_mut_ptr()), &mut size).is_ok();
        let _ = CloseHandle(h);
        if !ok {
            return String::new();
        }
        let path = String::from_utf16_lossy(&buf[..(size as usize).min(buf.len())]);
        path.rsplit(['\\', '/']).next().unwrap_or("").to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn verification() {
        assert!(typed_text_landed("", "hello world ", "hello world "));
        assert!(typed_text_landed("Dear Bob,\r\n", "Dear Bob,\r\nhello   world", "hello world "));
        // Already present before: only the growth rule can confirm it.
        assert!(!typed_text_landed("hello world", "hello world", "hello world "));
        assert!(typed_text_landed("hello world", "hello world hello world", "hello world "));
        // App autocorrected a word: still grew enough.
        assert!(typed_text_landed("abc", "abc Helo world", "hello world "));
        assert!(!typed_text_landed("abc", "abc", "hello world "));
    }
}
