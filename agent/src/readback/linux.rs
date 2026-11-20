//! AT-SPI2 read-back. Creating this backend turns on the session's accessibility
//! and screen-reader status flags so toolkits expose their text controls. These
//! flags are session-wide and are deliberately not turned off on drop.

use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use atspi::{
    object_match::{MatchType, ObjectMatchRule, SortOrder},
    object_ref::ObjectRefOwned,
    proxy::{
        accessible::AccessibleProxy, application::ApplicationProxy, bus::StatusProxy,
        collection::CollectionProxy, editable_text::EditableTextProxy, text::TextProxy,
    },
    AccessibilityConnection, Interface, State,
};
use atspi::zbus::{self, proxy::CacheProperties};

use super::{FieldSnapshot, Readback};

const CALL_TIMEOUT: Duration = Duration::from_millis(500);
const SEARCH_BUDGET: Duration = Duration::from_millis(950);
/// Budget for the calls on the control once found (insert, caret, verification).
const ACTION_BUDGET: Duration = Duration::from_millis(2_000);
const MAX_NODES: usize = 2_000;
const MAX_TEXT_BYTES: usize = 200_000;

pub struct AtspiReadback {
    runtime: tokio::runtime::Runtime,
    connection: AccessibilityConnection,
}

impl AtspiReadback {
    pub fn new() -> Result<Self> {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .context("create AT-SPI runtime")?;
        let connection = runtime.block_on(async {
            // org.a11y.Status lives on the session bus; GetAddress connects us
            // separately to the AT-SPI bus. Do not clear either flag on exit:
            // another accessibility client may also need it.
            let session = tokio::time::timeout(CALL_TIMEOUT, zbus::Connection::session())
                .await.context("session bus timeout")??;
            let status = tokio::time::timeout(CALL_TIMEOUT, StatusProxy::new(&session))
                .await.context("accessibility status timeout")??;
            if !tokio::time::timeout(CALL_TIMEOUT, status.is_enabled())
                .await.context("read IsEnabled timeout")?? {
                tokio::time::timeout(CALL_TIMEOUT, status.set_is_enabled(true))
                    .await.context("enable accessibility timeout")??;
            }
            if !tokio::time::timeout(CALL_TIMEOUT, status.screen_reader_enabled())
                .await.context("read ScreenReaderEnabled timeout")?? {
                tokio::time::timeout(CALL_TIMEOUT, status.set_screen_reader_enabled(true))
                    .await.context("enable screen reader timeout")??;
            }
            tokio::time::timeout(Duration::from_secs(2), AccessibilityConnection::new())
                .await.context("AT-SPI bus connection timeout")?
                .context("connect to AT-SPI bus")
        })?;
        Ok(Self { runtime, connection })
    }
}

// Do not let an unresponsive application hold up the agent. The aggregate
// deadline also bounds a deep tree or many failing applications.
async fn bounded<T>(deadline: Instant, future: impl Future<Output = zbus::Result<T>>) -> Option<T> {
    let remaining = deadline.checked_duration_since(Instant::now())?;
    tokio::time::timeout(remaining.min(CALL_TIMEOUT), future).await.ok()?.ok()
}

use std::future::Future;

async fn accessible<'a>(
    connection: &'a zbus::Connection,
    object: &ObjectRefOwned,
    deadline: Instant,
) -> Option<AccessibleProxy<'a>> {
    let proxy = AccessibleProxy::builder(connection)
        .destination(object.name()?.clone()).ok()?
        .path(object.path().clone()).ok()?
        .cache_properties(CacheProperties::No);
    bounded(deadline, proxy.build()).await
}

async fn snapshot(
    connection: &zbus::Connection,
    object: &ObjectRefOwned,
    app: &str,
    deadline: Instant,
) -> Option<FieldSnapshot> {
    let node = accessible(connection, object, deadline).await?;
    let state = bounded(deadline, node.get_state()).await?;
    if !state.contains(State::Focused) || !state.contains(State::Editable) {
        return None;
    }
    if !bounded(deadline, node.get_interfaces()).await?.contains(Interface::Text) {
        return None;
    }
    let text_proxy = bounded(deadline, TextProxy::builder(connection)
        .destination(object.name()?.clone()).ok()?
        .path(object.path().clone()).ok()?
        .cache_properties(CacheProperties::No).build()).await?;
    let characters = bounded(deadline, text_proxy.character_count()).await?.max(0);
    // AT-SPI offsets count Unicode characters, not bytes. Read at most 200k
    // characters, then enforce the byte limit at a UTF-8 boundary below.
    let mut text = bounded(deadline, text_proxy.get_text(0, characters.min(MAX_TEXT_BYTES as i32))).await?;
    if text.len() > MAX_TEXT_BYTES {
        let mut end = MAX_TEXT_BYTES;
        while !text.is_char_boundary(end) { end -= 1; }
        text.truncate(end);
    }
    let caret = bounded(deadline, text_proxy.caret_offset()).await.and_then(|n| usize::try_from(n).ok());
    Some(FieldSnapshot {
        app: app.to_owned(),
        owner: object.name()?.as_str().to_owned(),
        control_id: format!("{}{}", object.name()?.as_str(), object.path().as_str()),
        text,
        caret,
    })
}

async fn app_name(connection: &zbus::Connection, app: &ObjectRefOwned, deadline: Instant) -> String {
    if let Some(node) = accessible(connection, app, deadline).await {
        if let Some(name) = bounded(deadline, node.name()).await.filter(|s| !s.is_empty()) {
            return name;
        }
    }
    if let (Some(name), path) = (app.name(), app.path()) {
        if let Some(proxy) = bounded(deadline, ApplicationProxy::builder(connection)
            .destination(name.as_str()).expect("valid AT-SPI bus name")
            .path(path.as_str()).expect("valid AT-SPI object path")
            .cache_properties(CacheProperties::No).build()).await {
            if let Some(toolkit) = bounded(deadline, proxy.toolkit_name()).await.filter(|s| !s.is_empty()) {
                return toolkit;
            }
        }
        return name.to_string();
    }
    "unknown".into()
}

async fn find_in_app(
    connection: &zbus::Connection,
    app: &ObjectRefOwned,
    deadline: Instant,
) -> Option<(ObjectRefOwned, FieldSnapshot)> {
    let root = accessible(connection, app, deadline).await?;
    // Qt advertises Collection but GetMatches always returns an empty list, so it
    // must be walked. Its focused widget is always inside an Active top-level window,
    // which keeps the walk away from large inactive trees such as plasmashell.
    let qt = toolkit_name(connection, app, deadline).await.is_some_and(|t| is_qt(&t));
    // Collection is a server-side tree search; avoid building every proxy on
    // each keystroke. Some toolkits expose no Collection, hence the DFS below.
    if let Some(ifaces) = bounded(deadline, root.get_interfaces()).await.filter(|_| !qt) {
        if ifaces.contains(Interface::Collection) {
            if let (Some(name), path) = (app.name(), app.path()) {
                if let Some(collection) = bounded(deadline, CollectionProxy::builder(connection)
                    .destination(name.as_str()).ok()?
                    .path(path.as_str()).ok()?
                    .cache_properties(CacheProperties::No).build()).await {
                    let rule = ObjectMatchRule::builder()
                        .states([State::Focused], MatchType::All).build();
                    if let Some(matches) = bounded(deadline, collection.get_matches(rule, SortOrder::Canonical, 16, true)).await {
                        for node in matches {
                            if let Some(result) = snapshot(connection, &node, &app_name(connection, app, deadline).await, deadline).await {
                                return Some((node, result));
                            }
                        }
                        // The toolkit completed the search: avoid walking its
                        // entire tree if no editable text control matched.
                        return None;
                    }
                }
            }
        }
    }
    let mut stack = vec![(app.clone(), 0usize)];
    let mut seen = std::collections::HashSet::new();
    while let Some((node_ref, depth)) = stack.pop() {
        if seen.len() >= MAX_NODES || Instant::now() >= deadline { break; }
        let key = (node_ref.name().map(ToString::to_string), node_ref.path().to_string());
        if !seen.insert(key) { continue; }
        let Some(node) = accessible(connection, &node_ref, deadline).await else { continue };
        if let Some(state) = bounded(deadline, node.get_state()).await {
            if qt && depth == 1 && !state.contains(State::Active) {
                continue;
            }
            if state.contains(State::Focused) && state.contains(State::Editable) {
                if let Some(result) = snapshot(connection, &node_ref, &app_name(connection, app, deadline).await, deadline).await {
                    return Some((node_ref, result));
                }
            }
        }
        if let Some(children) = bounded(deadline, node.get_children()).await {
            stack.extend(children.into_iter().filter(|c| !c.is_null()).take(MAX_NODES - seen.len()).map(|c| (c, depth + 1)));
        }
    }
    None
}

/// The focused editable Text control, with the refs needed to act on it.
struct Focused {
    object: ObjectRefOwned,
    app: ObjectRefOwned,
    snapshot: FieldSnapshot,
}

async fn find_focused(connection: &AccessibilityConnection, deadline: Instant) -> Result<Option<Focused>> {
    let root = match tokio::time::timeout(CALL_TIMEOUT, connection.root_accessible_on_registry()).await {
        Ok(Ok(root)) => root,
        Ok(Err(err)) => return Err(anyhow::Error::new(err).context("AT-SPI desktop root")),
        Err(_) => return Err(anyhow::anyhow!("AT-SPI desktop root timed out")),
    };
    let apps = bounded(deadline, root.get_children()).await.unwrap_or_default();
    for app in apps {
        if Instant::now() >= deadline { break; }
        if let Some((object, snapshot)) = find_in_app(connection.connection(), &app, deadline).await {
            return Ok(Some(Focused { object, app, snapshot }));
        }
    }
    Ok(None)
}

async fn toolkit_name(connection: &zbus::Connection, app: &ObjectRefOwned, deadline: Instant) -> Option<String> {
    let proxy = bounded(deadline, ApplicationProxy::builder(connection)
        .destination(app.name()?.clone()).ok()?
        .path(app.path().clone()).ok()?
        .cache_properties(CacheProperties::No).build()).await?;
    bounded(deadline, proxy.toolkit_name()).await
}

fn is_qt(toolkit: &str) -> bool {
    toolkit.to_ascii_lowercase().starts_with("qt")
}

/// `(length, caret_advance)` arguments for inserting `text`, in the units the
/// toolkit's AT-SPI bridge expects.
///
/// ATK-based bridges (at-spi2-atk: GTK 3, Firefox, Chromium) and GTK 4 pass the
/// InsertText length on to `gtk_editable_insert_text`-style APIs as UTF-8 bytes and
/// count offsets in characters. Qt resizes the QString to `length`, so both are UTF-16
/// code units there. The conventions agree for ASCII, so an unknown toolkit is only
/// accepted for ASCII text.
fn insert_units(text: &str, toolkit: Option<&str>) -> Option<(i32, i32)> {
    let chars = text.chars().count();
    let utf16 = text.encode_utf16().count();
    let (length, advance) = match toolkit {
        Some(t) if is_qt(t) => (utf16, utf16),
        Some(_) => (text.len(), chars),
        None if text.is_ascii() => (text.len(), chars),
        None => return None,
    };
    Some((i32::try_from(length).ok()?, i32::try_from(advance).ok()?))
}

/// Insert `text` at the caret of the focused editable control. `Ok(false)` means the
/// field was not changed. With `require_empty`, only an empty control is touched
/// (live test safety).
async fn insert_at_focus(connection: &AccessibilityConnection, text: &str, require_empty: bool) -> Result<bool> {
    let Some(found) = find_focused(connection, Instant::now() + SEARCH_BUDGET).await? else {
        tracing::debug!("atspi insert: no focused editable text control");
        return Ok(false);
    };
    if require_empty && !found.snapshot.text.is_empty() {
        tracing::debug!("atspi insert: focused control is not empty, skipped");
        return Ok(false);
    }
    let conn = connection.connection();
    let deadline = Instant::now() + ACTION_BUDGET;
    let object = &found.object;
    let node = accessible(conn, object, deadline).await.context("accessible proxy")?;
    let interfaces = bounded(deadline, node.get_interfaces()).await.context("read interfaces")?;
    if !interfaces.contains(Interface::EditableText) {
        tracing::debug!("atspi insert: {} does not implement EditableText", found.snapshot.control_id);
        return Ok(false);
    }
    let name = object.name().context("object without bus name")?;
    let text_proxy = bounded(deadline, TextProxy::builder(conn)
        .destination(name.clone())?
        .path(object.path().clone())?
        .cache_properties(CacheProperties::No).build()).await.context("Text proxy")?;
    let editable = bounded(deadline, EditableTextProxy::builder(conn)
        .destination(name.clone())?
        .path(object.path().clone())?
        .cache_properties(CacheProperties::No).build()).await.context("EditableText proxy")?;
    // Read the caret again right before inserting; the search result may be ~1 s old.
    let caret = match bounded(deadline, text_proxy.caret_offset()).await {
        Some(c) => c,
        None => found.snapshot.caret.and_then(|c| i32::try_from(c).ok()).context("caret offset unavailable")?,
    };
    anyhow::ensure!(caret >= 0, "control has no caret");
    let toolkit = toolkit_name(conn, &found.app, deadline).await;
    let (length, advance) = insert_units(text, toolkit.as_deref())
        .with_context(|| format!("unknown toolkit ({toolkit:?}) for non-ASCII insert"))?;
    let before = bounded(deadline, text_proxy.character_count()).await;

    let inserted = tokio::time::timeout(CALL_TIMEOUT, editable.insert_text(caret, text, length)).await;
    let accepted = match inserted {
        Ok(Ok(true)) => true,
        other => {
            // A timed-out reply or a bridge that reports failure may still have
            // changed the text. Pasting as well would then insert it twice.
            let after = bounded(deadline, text_proxy.character_count()).await;
            match (before, after) {
                (Some(b), Some(a)) if a > b => {
                    tracing::warn!("atspi insert: InsertText returned {other:?} but the text grew by {} chars; treating as inserted", a - b);
                    true
                }
                _ => {
                    tracing::debug!("atspi insert: InsertText returned {other:?}; text unchanged or unreadable");
                    false
                }
            }
        }
    };
    if !accepted {
        return Ok(false);
    }
    // GTK leaves the caret where it was; Qt already moved it. Either way, put it
    // after the inserted text. Best effort: the text is in.
    match bounded(deadline, text_proxy.set_caret_offset(caret + advance)).await {
        Some(true) => {}
        other => tracing::debug!("atspi insert: SetCaretOffset({}) returned {other:?}", caret + advance),
    }
    tracing::debug!("atspi insert: {} chars into {} ({:?}, toolkit {toolkit:?})",
        text.chars().count(), found.snapshot.control_id, found.snapshot.app);
    Ok(true)
}

impl AtspiReadback {
    fn insert(&mut self, text: &str, require_empty: bool) -> Result<bool> {
        if text.is_empty() {
            return Ok(false);
        }
        let connection = self.connection.clone();
        match self.runtime.block_on(insert_at_focus(&connection, text, require_empty)) {
            Ok(done) => Ok(done),
            Err(e) => {
                tracing::debug!("atspi insert unavailable: {e:#}");
                Ok(false)
            }
        }
    }
}

impl Readback for AtspiReadback {
    fn name(&self) -> &'static str { "atspi" }

    fn focused_text(&mut self) -> Result<Option<FieldSnapshot>> {
        let deadline = Instant::now() + SEARCH_BUDGET;
        let connection = self.connection.clone();
        self.runtime.block_on(async {
            Ok(find_focused(&connection, deadline).await?.map(|f| f.snapshot))
        })
    }

    fn insert_at_caret(&mut self, text: &str) -> Result<bool> {
        self.insert(text, false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    #[ignore = "requires a live graphical desktop and AT-SPI bus"]
    fn readback_live() -> Result<()> {
        let mut backend = AtspiReadback::new()?;
        match backend.focused_text()? {
            Some(field) => println!("app={:?} control_id={} text={:?} caret={:?}",
                field.app, field.control_id, field.text.chars().take(120).collect::<String>(), field.caret),
            None => println!("readback: None (no focused editable AT-SPI Text control)"),
        }
        Ok(())
    }

    #[test]
    fn insert_units_per_toolkit() {
        // "é" is 1 char, 2 UTF-8 bytes, 1 UTF-16 unit; the emoji is 1 char, 4 bytes, 2 units.
        let text = "café \u{1F600} ";
        assert_eq!(insert_units(text, Some("GTK")), Some((11, 7)));
        assert_eq!(insert_units(text, Some("Gecko")), Some((11, 7)));
        assert_eq!(insert_units(text, Some("Qt")), Some((8, 8)));
        assert_eq!(insert_units(text, None), None);
        assert_eq!(insert_units("hello ", None), Some((6, 6)));
        assert_eq!(insert_units("hello ", Some("Qt")), Some((6, 6)));
    }

    /// Inserts a marker, but only into a focused control that is empty (checked in the
    /// same search that performs the insert), so it never touches a real document.
    #[test]
    #[ignore = "requires a live graphical desktop and AT-SPI bus; focus an EMPTY text field"]
    fn insert_live() -> Result<()> {
        // Non-ASCII on purpose: exercises the per-toolkit length units.
        const MARKER: &str = "transcriptd-insert-test café \u{1F600} ";
        let mut backend = AtspiReadback::new()?;
        match backend.focused_text()? {
            None => println!("insert: skipped, no focused editable AT-SPI Text control"),
            Some(field) if !field.text.is_empty() => println!(
                "insert: skipped, focused control in {:?} is not empty ({} chars)", field.app, field.text.chars().count()),
            Some(field) => {
                let inserted = backend.insert(MARKER, true)?;
                let after = backend.focused_text()?;
                println!("insert: app={:?} control_id={} inserted={inserted}", field.app, field.control_id);
                match after {
                    Some(a) => {
                        println!("insert: after text={:?} caret={:?}", a.text, a.caret);
                        if inserted {
                            assert_eq!(a.text, MARKER);
                        }
                    }
                    None => println!("insert: after: None"),
                }
            }
        }
        Ok(())
    }
}
