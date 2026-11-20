//! The agent state machine: hotkeys -> capture -> server -> inject, driven by stdin
//! commands and emitting stdout events. Also hosts the single-instance socket so
//! `transcriptd-agent toggle` works from compositor keybindings.

use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::sync::{mpsc, Mutex};

use crate::audio::{self, Capture};
use crate::config::{AgentConfig, HotkeyMode};
use crate::hotkey::{self, Chord, HotkeyEvent, HotkeyId, HotkeySpec};
use crate::inject::{self, Injector};
use crate::protocol::{Command, CorrectionPair, Event};
use crate::readback::diff::{observe, Observation};
use crate::readback::{self, focus_check, FieldSnapshot, FocusCheck, FocusId, Readback};
use crate::server::{self, DictateSession, MeetingSession, ServerEvent};

pub struct App {
    cfg: AgentConfig,
    events: mpsc::Sender<Event>,
    injector: Arc<Mutex<Option<Box<dyn Injector>>>>,
    inject_backend: &'static str,
    hotkey_backend: &'static str,
    recording: Option<Recording>,
    meeting: Option<Meeting>,
    readback: Option<Arc<Mutex<Box<dyn Readback>>>>,
    readback_backend: &'static str,
    /// Watches the field we last pasted into for user corrections.
    watch: Option<tokio::task::JoinHandle<()>>,
}

/// How long the accessibility layer may take to identify the focused field, at
/// hotkey press and again right before injecting.
const FOCUS_LOOKUP: Duration = Duration::from_millis(1000);
/// Meetings: most "them" frames allowed to queue behind "me" before the oldest are
/// dropped, so a loopback clock running fast cannot push the channels apart (200 ms).
const MAX_THEM_BACKLOG: usize = 10;
/// Presses shorter than this (key down to key up) are taken as accidental and dropped.
const MIN_PRESS: Duration = Duration::from_millis(150);

struct Recording {
    session: DictateSession,
    _capture: Capture,
    /// When the key went down (or the start command arrived), before the microphone
    /// and the connection were set up.
    pressed: Instant,
    pump: tokio::task::JoinHandle<()>,
    /// The focused field at hotkey press, if it could be identified.
    focus: Option<FocusId>,
    /// Set when the take ends: give up on it (audio still flushing, or no `final`) at
    /// this instant.
    final_deadline: Option<Instant>,
}

struct Meeting {
    id: Option<i64>,
    session: Option<MeetingSession>,
    stop: tokio::sync::oneshot::Sender<()>,
    done: tokio::task::JoinHandle<Result<(String, u64)>>,
}

pub async fn run(cfg: AgentConfig, stdio: bool) -> Result<()> {
    let (ev_tx, mut ev_rx) = mpsc::channel::<Event>(256);
    // stdout writer
    tokio::spawn(async move {
        let mut out = tokio::io::stdout();
        while let Some(ev) = ev_rx.recv().await {
            let mut line = serde_json::to_string(&ev).unwrap();
            line.push('\n');
            if out.write_all(line.as_bytes()).await.is_err() {
                break;
            }
            let _ = out.flush().await;
        }
    });

    let (cmd_tx, mut cmd_rx) = mpsc::channel::<Command>(64);
    if stdio {
        let tx = cmd_tx.clone();
        tokio::spawn(async move {
            let mut lines = BufReader::new(tokio::io::stdin()).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                if line.trim().is_empty() {
                    continue;
                }
                match serde_json::from_str::<Command>(&line) {
                    Ok(c) => {
                        if tx.send(c).await.is_err() {
                            break;
                        }
                    }
                    Err(e) => tracing::warn!("bad command {line:?}: {e}"),
                }
            }
            let _ = tx.send(Command::Quit).await;
        });
    }
    if let Err(e) = crate::ipc::serve_single_instance(cmd_tx.clone()).await {
        let _ = ev_tx.send(Event::Error { message: format!("{e:#}"), fatal: true }).await;
        tokio::time::sleep(Duration::from_millis(50)).await;
        return Err(e);
    }

    let mut app = App::new(cfg, ev_tx.clone()).await?;
    let (hk_tx, hk_rx) = crossbeam_channel::unbounded::<HotkeyEvent>();
    app.hotkey_backend = app.start_hotkeys(hk_tx)?;
    app.emit(Event::Hello {
        version: env!("CARGO_PKG_VERSION").into(),
        platform: std::env::consts::OS.into(),
        hotkey_backend: app.hotkey_backend.into(),
        inject_backend: app.inject_backend.into(),
        readback_backend: app.readback_backend.into(),
    })
    .await;

    // Bridge the crossbeam hotkey channel into tokio, stamping each event as it arrives:
    // the loop may be busy (connecting to the server) when the key goes up.
    let (hk_async_tx, mut hk_async_rx) = mpsc::channel::<(HotkeyEvent, Instant)>(64);
    std::thread::spawn(move || {
        while let Ok(ev) = hk_rx.recv() {
            if hk_async_tx.blocking_send((ev, Instant::now())).is_err() {
                break;
            }
        }
    });

    loop {
        let final_deadline = app.recording.as_ref().and_then(|r| r.final_deadline);
        tokio::select! {
            _ = sleep_until(final_deadline) => app.final_timed_out().await,
            Some(cmd) = cmd_rx.recv() => {
                if matches!(cmd, Command::Quit) { break; }
                if let Err(e) = app.handle(cmd).await {
                    app.emit(Event::Error { message: e.to_string(), fatal: false }).await;
                }
            }
            Some((hk, at)) = hk_async_rx.recv() => {
                if let Err(e) = app.hotkey(hk, at).await {
                    app.emit(Event::Error { message: e.to_string(), fatal: false }).await;
                }
            }
            Some(ev) = recording_event(&mut app.recording) => {
                if let Err(e) = app.server_event(ev).await {
                    app.emit(Event::Error { message: e.to_string(), fatal: false }).await;
                }
            }
            Some(ev) = meeting_event(&mut app.meeting) => app.meeting_server_event(app.meeting.as_ref().and_then(|m| m.id), ev).await,
        }
    }
    if app.meeting.is_some() {
        let _ = app.stop_meeting().await;
    }
    crate::ipc::cleanup();
    // Blocking stdin/hotkey threads would keep the runtime alive; flush and leave.
    tokio::time::sleep(Duration::from_millis(50)).await;
    std::process::exit(0);
}

async fn recording_event(rec: &mut Option<Recording>) -> Option<ServerEvent> {
    match rec {
        Some(r) => r.session.events.recv().await,
        None => std::future::pending().await,
    }
}

async fn sleep_until(deadline: Option<Instant>) {
    match deadline {
        Some(d) => tokio::time::sleep_until(d.into()).await,
        None => std::future::pending().await,
    }
}

async fn meeting_event(meeting: &mut Option<Meeting>) -> Option<ServerEvent> {
    match meeting.as_mut().and_then(|m| m.session.as_mut()) {
        Some(session) => match session.events.lock().await.recv().await {
            Some(ev) => Some(ev),
            None => std::future::pending().await,
        },
        None => std::future::pending().await,
    }
}

impl App {
    async fn new(cfg: AgentConfig, events: mpsc::Sender<Event>) -> Result<Self> {
        let (injector, inject_backend) = match inject::new_injector(&cfg.virtual_device_name) {
            Ok(i) => {
                let n = i.name();
                (Some(i), n)
            }
            Err(e) => {
                tracing::warn!("injection unavailable: {e:#}");
                let _ = events.send(Event::Error { message: format!("text injection unavailable: {e:#}"), fatal: false }).await;
                (None, "none")
            }
        };
        // The AT-SPI backend drives its own runtime with block_on, which panics on a
        // thread that is running async tasks; step out of the runtime for it.
        let (readback, readback_backend) = match tokio::task::block_in_place(readback::new_readback) {
            Some(r) => {
                let n = r.name();
                (Some(Arc::new(Mutex::new(r))), n)
            }
            None => (None, "none"),
        };
        Ok(Self {
            cfg,
            events,
            readback,
            readback_backend,
            watch: None,
            injector: Arc::new(Mutex::new(injector)),
            inject_backend,
            hotkey_backend: "none",
            recording: None,
            meeting: None,
        })
    }

    fn start_hotkeys(&self, tx: crossbeam_channel::Sender<HotkeyEvent>) -> Result<&'static str> {
        let spec = HotkeySpec {
            ptt: Chord::parse(&self.cfg.hotkey)?,
            correction: self.cfg.correction_hotkey.as_deref().map(Chord::parse).transpose()?,
            ignore_device_name: self.cfg.virtual_device_name.clone(),
        };
        match hotkey::start(spec, tx) {
            Ok(name) => Ok(name),
            Err(e) => {
                tracing::warn!("hotkeys unavailable: {e:#}");
                let ev = self.events.clone();
                let msg = format!("global hotkey unavailable: {e:#}");
                tokio::spawn(async move {
                    let _ = ev.send(Event::Error { message: msg, fatal: false }).await;
                });
                Ok("none")
            }
        }
    }

    async fn emit(&self, ev: Event) {
        let _ = self.events.send(ev).await;
    }

    async fn handle(&mut self, cmd: Command) -> Result<()> {
        match cmd {
            Command::Configure { config } => {
                let hotkey_changed = config.hotkey != self.cfg.hotkey || config.correction_hotkey != self.cfg.correction_hotkey;
                self.cfg = config;
                let _ = self.cfg.save();
                let mut warnings = Vec::new();
                if hotkey_changed {
                    warnings.push("hotkey changes take effect after restarting the agent".into());
                }
                self.emit(Event::Configured { warnings }).await;
            }
            Command::PttStart => self.start_recording(Instant::now()).await?,
            Command::PttStop => self.stop_recording(Instant::now()).await?,
            Command::PttToggle => {
                if self.recording.is_some() {
                    self.stop_recording(Instant::now()).await?
                } else {
                    self.start_recording(Instant::now()).await?
                }
            }
            Command::PttCancel => self.cancel_recording().await,
            Command::Inject { text } => {
                let method = self.inject_text(&text).await?;
                self.emit(Event::Injected { chars: text.chars().count(), method: method.into() }).await;
                self.start_watch(None, text);
            }
            Command::ClipboardRead => self.emit(Event::Clipboard { text: inject::clipboard_read() }).await,
            Command::SelectionRead => {
                let inj = self.injector.clone();
                let text = tokio::task::spawn_blocking(move || {
                    let mut g = inj.blocking_lock();
                    match g.as_mut() {
                        Some(i) => inject::read_selection(i.as_mut()),
                        None => Ok(None),
                    }
                })
                .await??;
                self.emit(Event::Selection { text }).await;
            }
            Command::DevicesList => {
                let (inputs, outputs) = tokio::task::spawn_blocking(audio::mic::list).await?;
                self.emit(Event::Devices { inputs, outputs }).await;
            }
            Command::MeetingStart { meeting_id, title } => self.start_meeting(meeting_id, title).await?,
            Command::MeetingStop => self.stop_meeting().await?,
            Command::Ping => self.emit(Event::Pong).await,
            Command::Quit => {}
        }
        Ok(())
    }

    /// `at` is when the hotkey thread reported the event.
    async fn hotkey(&mut self, ev: HotkeyEvent, at: Instant) -> Result<()> {
        match (ev, self.cfg.hotkey_mode) {
            (HotkeyEvent::Down(HotkeyId::Ptt), HotkeyMode::Hold) => {
                self.emit(Event::PttDown { source: "hotkey".into() }).await;
                self.start_recording(at).await?
            }
            (HotkeyEvent::Up(HotkeyId::Ptt), HotkeyMode::Hold) => {
                self.emit(Event::PttUp).await;
                self.stop_recording(at).await?
            }
            (HotkeyEvent::Down(HotkeyId::Ptt), HotkeyMode::Toggle) => {
                if self.recording.is_some() {
                    self.emit(Event::PttUp).await;
                    self.stop_recording(at).await?
                } else {
                    self.emit(Event::PttDown { source: "hotkey".into() }).await;
                    self.start_recording(at).await?
                }
            }
            (HotkeyEvent::Up(HotkeyId::Ptt), HotkeyMode::Toggle) => {}
            (HotkeyEvent::Down(HotkeyId::Correction), _) => {
                // Copy the selection and hand it to the client, which diffs it against
                // recent dictations and decides whether to offer a vocab entry.
                self.handle(Command::SelectionRead).await?
            }
            (HotkeyEvent::Up(HotkeyId::Correction), _) => {}
        }
        Ok(())
    }

    /// `pressed`: when the key went down, so a slow connection does not count against
    /// the press (see [`MIN_PRESS`]).
    async fn start_recording(&mut self, pressed: Instant) -> Result<()> {
        if self.recording.is_some() {
            return Ok(());
        }
        self.stop_watch();
        // Microphone first: its channel buffers ~5 s, so the first words are not lost
        // while we look up the focused field and connect to the server.
        let capture = tokio::task::spawn_blocking({
            let dev = self.cfg.mic_device.clone();
            move || audio::mic::start(dev.as_deref())
        })
        .await?
        .context("start microphone")?;
        // Snapshot the field we are about to type into: the server uses the surrounding
        // text for document vocabulary, spacing/case, and profile-by-script; its id is
        // checked again before injecting.
        let snapshot = self.focused_field().await;
        let focus = snapshot.as_ref().map(FieldSnapshot::focus_id);
        let context = snapshot.as_ref().map(field_context);
        let app_hint = snapshot.as_ref().map(|s| s.app.clone());
        let session = DictateSession::open(&self.cfg, app_hint, context).await.context("open dictation session")?;
        // Pump frames from the capture thread into the WS writer; report levels.
        let rx = capture.rx.clone();
        let session_tx = session.audio_tx_clone();
        let ev = self.events.clone();
        let pump = tokio::task::spawn_blocking(move || {
            let mut last_level = Instant::now();
            while let Ok(frame) = rx.recv() {
                if last_level.elapsed() > Duration::from_millis(100) {
                    last_level = Instant::now();
                    let _ = ev.blocking_send(Event::Level { db: audio::rms_db(&frame) });
                }
                if session_tx.blocking_send(frame).is_err() {
                    break;
                }
            }
        });
        self.recording = Some(Recording { session, _capture: capture, pressed, pump, focus, final_deadline: None });
        Ok(())
    }

    /// `released`: when the key went up.
    async fn stop_recording(&mut self, released: Instant) -> Result<()> {
        let Some(rec) = self.recording.as_mut() else { return Ok(()) };
        if rec.final_deadline.is_some() {
            return Ok(()); // already ended, waiting for "final"
        }
        // Very short press: treat as accidental.
        if is_short_press(rec.pressed, released) {
            self.cancel_recording().await;
            return Ok(());
        }
        // Arm the deadline now, so flushing the audio counts against it too: a writer
        // stuck on a dead connection with a full queue must not wedge the agent. The
        // select loop keeps reading the session's events until "final" or the deadline.
        rec.final_deadline = Some(Instant::now() + Duration::from_secs(self.cfg.final_timeout_secs.max(1)));
        // Stop capture (drops the stream), then send "end" once the pump has queued the
        // last frame, without waiting for it here.
        rec._capture = dummy_capture();
        let pump = std::mem::replace(&mut rec.pump, tokio::spawn(async {}));
        rec.session.end_after(async move {
            let _ = pump.await;
        });
        Ok(())
    }

    async fn cancel_recording(&mut self) {
        if let Some(rec) = self.recording.take() {
            rec.session.cancel().await;
        }
    }

    async fn final_timed_out(&mut self) {
        if let Some(rec) = self.recording.take() {
            rec.session.cancel().await;
            let secs = self.cfg.final_timeout_secs.max(1);
            self.emit(Event::Error { message: format!("the server did not take the audio and return the transcript within {secs} s of the key going up; the take was dropped"), fatal: false }).await;
        }
    }

    async fn server_event(&mut self, ev: ServerEvent) -> Result<()> {
        match ev {
            ServerEvent::Ready | ServerEvent::MeetingSegment(_) => {}
            ServerEvent::Partial(text) => self.emit(Event::Partial { text }).await,
            ServerEvent::Final(result) => {
                let focus = self.recording.take().and_then(|r| r.focus);
                let text = result.get("text").and_then(|t| t.as_str()).unwrap_or("").to_string();
                // `insert_text` is `text` fitted to the caret (leading/trailing space, case).
                let to_insert = result.get("insert_text").and_then(|t| t.as_str()).filter(|t| !t.trim().is_empty()).map(|t| t.to_string()).unwrap_or_else(|| text.clone());
                let mut injected = false;
                let focus_moved = self.cfg.auto_inject && !text.is_empty() && focus.is_some() && {
                    let now = self.focused_field().await.map(|s| s.focus_id());
                    focus_check(focus.as_ref(), now.as_ref()) == FocusCheck::Moved
                };
                if focus_moved {
                    // Never type into a field the user did not dictate into.
                    let copy = text.clone();
                    let copied = tokio::task::spawn_blocking(move || inject::clipboard_write(&copy)).await.map_err(anyhow::Error::from).and_then(|r| r);
                    let message = match copied {
                        Ok(()) => "focus moved during dictation, so the text was not typed; it is on the clipboard".to_string(),
                        Err(e) => format!("focus moved during dictation, so the text was not typed (copying it to the clipboard failed: {e:#})"),
                    };
                    self.emit(Event::Warning { message, fatal: false, code: "focus_changed" }).await;
                } else if self.cfg.auto_inject && !text.is_empty() {
                    match self.inject_text(&to_insert).await {
                        Ok(method) => {
                            injected = true;
                            self.emit(Event::Injected { chars: text.chars().count(), method: method.into() }).await;
                            self.start_watch(result.get("dictation_id").and_then(|v| v.as_i64()), text.clone());
                        }
                        Err(e) => self.emit(Event::Error { message: format!("inject failed: {e:#}"), fatal: false }).await,
                    }
                }
                self.emit(Event::Final { result, injected }).await;
            }
            ServerEvent::Error(m) => {
                self.recording = None;
                self.emit(Event::Error { message: m, fatal: false }).await;
            }
            ServerEvent::Closed => {
                // After "final", "error" or a cancel the recording is already gone.
                if self.recording.take().is_some() {
                    self.emit(Event::Error { message: "the server closed the dictation connection before the transcript arrived".into(), fatal: false }).await;
                }
            }
        }
        Ok(())
    }

    async fn meeting_server_event(&self, meeting_id: Option<i64>, ev: ServerEvent) {
        match ev {
            ServerEvent::MeetingSegment(segment) => self.emit(Event::MeetingSegment { meeting_id, segment }).await,
            ServerEvent::Error(message) => self.emit(Event::Error { message, fatal: false }).await,
            _ => {}
        }
    }

    /// The focused editable field, if the accessibility layer can see it in time.
    async fn focused_field(&self) -> Option<FieldSnapshot> {
        let rb = self.readback.clone()?;
        tokio::time::timeout(FOCUS_LOOKUP, tokio::task::spawn_blocking(move || rb.blocking_lock().focused_text()))
            .await
            .ok()?
            .ok()?
            .ok()?
    }

    // -- read-back: learn corrections from the target app ----------------------------------

    fn start_watch(&mut self, dictation_id: Option<i64>, pasted: String) {
        self.stop_watch();
        let Some(rb) = self.readback.clone() else { return };
        let events = self.events.clone();
        self.watch = Some(tokio::spawn(watch_pasted(rb, events, dictation_id, pasted)));
    }

    /// Stop the watcher, after one last immediate look so a correction made right
    /// before the next dictation is not lost.
    fn stop_watch(&mut self) {
        if let Some(h) = self.watch.take() {
            h.abort();
        }
    }

    async fn inject_text(&self, text: &str) -> Result<&'static str> {
        let inj = self.injector.clone();
        let rb = self.readback.clone();
        let cfg = self.cfg.inject.clone();
        let text = text.to_string();
        tokio::task::spawn_blocking(move || {
            let mut g = inj.blocking_lock();
            let i = g.as_mut().ok_or_else(|| anyhow::anyhow!("no injector available"))?;
            // Accessibility insertion first (AT-SPI / UIA-verified typing); clipboard fallback.
            let result = match rb.as_ref() {
                Some(r) => {
                    let mut g2 = r.blocking_lock();
                    inject::inject_with(i.as_mut(), &cfg, &text, Some(&mut **g2))
                }
                None => inject::inject_with(i.as_mut(), &cfg, &text, None),
            };
            result
        })
        .await?
    }

    // -- meetings ----------------------------------------------------------------------

    async fn start_meeting(&mut self, meeting_id: Option<i64>, title: Option<String>) -> Result<()> {
        if self.meeting.is_some() {
            return Ok(());
        }
        let mut meeting_id = meeting_id;
        if meeting_id.is_none() && !self.cfg.token.is_empty() {
            match server::post_json(&self.cfg, "/v1/meetings", serde_json::json!({
                "title": title.as_deref().unwrap_or(""),
                "profile": self.cfg.profile,
            })).await {
                Ok(meeting) => match meeting.get("id").and_then(|id| id.as_i64()) {
                    Some(id) => meeting_id = Some(id),
                    None => self.emit(Event::Error { message: "create meeting returned no id".into(), fatal: false }).await,
                },
                Err(e) => {
                    tracing::warn!("create meeting failed: {e:#}");
                    self.emit(Event::Error { message: format!("create meeting failed: {e:#}"), fatal: false }).await;
                }
            }
        }
        let session = if let Some(id) = meeting_id {
            if self.cfg.token.is_empty() {
                None
            } else {
                match MeetingSession::open(&self.cfg, id).await {
                    Ok(session) => Some(session),
                    Err(e) => {
                        tracing::warn!("meeting stream unavailable: {e:#}");
                        self.emit(Event::Error { message: format!("meeting stream unavailable: {e:#}"), fatal: false }).await;
                        None
                    }
                }
            }
        } else {
            None
        };
        let dir = self.cfg.meetings_dir();
        std::fs::create_dir_all(&dir)?;
        let stamp = chrono_lite::now_stamp();
        let slug = title.as_deref().map(slugify).filter(|s| !s.is_empty()).unwrap_or_else(|| "meeting".into());
        let path = dir.join(format!("{stamp}-{slug}.wav"));
        let mic_dev = self.cfg.mic_device.clone();
        let loop_dev = self.cfg.loopback_device.clone();
        let ev = self.events.clone();
        let (stop_tx, mut stop_rx) = tokio::sync::oneshot::channel::<()>();
        let path_str = path.to_string_lossy().to_string();
        let path2 = path.clone();
        let stream_session = session.clone();
        let done = tokio::task::spawn_blocking(move || -> Result<(String, u64)> {
            let mic = audio::mic::start(mic_dev.as_deref()).context("mic")?;
            let them = match audio::loopback::start(loop_dev.as_deref()) {
                Ok(c) => Some(c),
                Err(e) => {
                    let _ = ev.blocking_send(Event::Error { message: format!("system audio unavailable, recording mic only: {e:#}"), fatal: false });
                    None
                }
            };
            let mut wav = audio::wav::StereoWav::create(&path2)?;
            let mut last_level = Instant::now();
            let silence = vec![0i16; audio::FRAME];
            loop {
                if stop_rx.try_recv().is_ok() {
                    break;
                }
                let me = match mic.rx.recv_timeout(Duration::from_millis(200)) {
                    Ok(f) => f,
                    Err(crossbeam_channel::RecvTimeoutError::Timeout) => continue,
                    Err(_) => break,
                };
                // Pull whatever the loopback side has; pad with silence if it is behind,
                // and drop its oldest frames if it runs ahead (clock drift).
                if let Some(c) = them.as_ref() {
                    while c.rx.len() > MAX_THEM_BACKLOG {
                        let _ = c.rx.try_recv();
                    }
                }
                let other = them.as_ref().and_then(|c| c.rx.try_recv().ok()).unwrap_or_else(|| silence.clone());
                wav.write(&me, &other)?;
                if let Some(session) = &stream_session {
                    let n = me.len().max(other.len());
                    let mut interleaved = Vec::with_capacity(n * 2);
                    for i in 0..n {
                        interleaved.push(*me.get(i).unwrap_or(&0));
                        interleaved.push(*other.get(i).unwrap_or(&0));
                    }
                    session.send_frame(interleaved);
                }
                if last_level.elapsed() > Duration::from_millis(250) {
                    last_level = Instant::now();
                    let _ = ev.blocking_send(Event::MeetingLevel { me_db: audio::rms_db(&me), them_db: audio::rms_db(&other) });
                }
            }
            let dur = wav.duration_ms();
            wav.finish()?;
            Ok((path2.to_string_lossy().to_string(), dur))
        });
        self.meeting = Some(Meeting { id: meeting_id, session, stop: stop_tx, done });
        self.emit(Event::MeetingStarted { meeting_id, wav_path: path_str }).await;
        Ok(())
    }

    async fn stop_meeting(&mut self) -> Result<()> {
        let Some(mut m) = self.meeting.take() else { return Ok(()) };
        let _ = m.stop.send(());
        let (wav_path, duration_ms) = m.done.await??;
        if let Some(session) = &mut m.session {
            // The main select loop is paused here, so drain segments while waiting
            // for the server to flush its last segment and confirm "ended".
            let mut events = session.events.lock().await;
            let end = session.end();
            tokio::pin!(end);
            let mut receiving = true;
            loop {
                tokio::select! {
                    result = &mut end => {
                        if let Err(e) = result {
                            self.emit(Event::Error { message: format!("end meeting stream failed: {e:#}"), fatal: false }).await;
                        }
                        break;
                    }
                    ev = events.recv(), if receiving => {
                        match ev {
                            Some(ev) => self.meeting_server_event(m.id, ev).await,
                            None => receiving = false,
                        }
                    }
                }
            }
            while let Ok(ev) = events.try_recv() {
                self.meeting_server_event(m.id, ev).await;
            }
        }
        if let Some(id) = m.id {
            if !self.cfg.token.is_empty() {
                if let Err(e) = server::post_file(&self.cfg, &format!("/v1/meetings/{id}/audio"), std::path::Path::new(&wav_path)).await {
                    tracing::warn!("upload meeting WAV failed: {e:#}");
                    self.emit(Event::Error { message: format!("upload meeting WAV failed: {e:#}"), fatal: false }).await;
                }
            }
        }
        self.emit(Event::MeetingStopped { meeting_id: m.id, wav_path, duration_ms }).await;
        Ok(())
    }
}

/// Poll the focused field after a paste and report word substitutions the user makes.
async fn watch_pasted(rb: Arc<Mutex<Box<dyn Readback>>>, events: mpsc::Sender<Event>, dictation_id: Option<i64>, pasted: String) {
    use std::collections::HashSet;

    let mut reported: HashSet<(String, String)> = HashSet::new();
    let mut control: Option<FocusId> = None;
    let mut misses = 0u8;
    // First look shortly after the paste, then back off; total window about one minute.
    for delay_ms in [400u64, 1500, 2000, 3000, 5000, 8000, 10000, 15000, 15000] {
        tokio::time::sleep(Duration::from_millis(delay_ms)).await;
        let rb2 = rb.clone();
        let snap = tokio::task::spawn_blocking(move || rb2.blocking_lock().focused_text()).await;
        let snap = match snap {
            Ok(Ok(s)) => s,
            Ok(Err(e)) => {
                tracing::debug!("readback failed: {e:#}");
                None
            }
            Err(_) => None,
        };
        let Some(snap) = snap else {
            misses += 1;
            if misses >= 2 {
                break;
            }
            continue;
        };
        let now = snap.focus_id();
        match &control {
            Some(c) if focus_check(Some(c), Some(&now)) == FocusCheck::Moved => break, // focus moved to another field
            Some(_) => {}
            None => control = Some(now),
        }
        match observe(&pasted, &snap.text) {
            Observation::Gone => {
                misses += 1;
                if misses >= 2 {
                    break;
                }
            }
            Observation::Unchanged => misses = 0,
            Observation::Edited { corrections, .. } => {
                misses = 0;
                let fresh: Vec<CorrectionPair> = corrections
                    .into_iter()
                    .filter(|c| reported.insert((c.heard.clone(), c.meant.clone())))
                    .map(|c| CorrectionPair { heard: c.heard, meant: c.meant })
                    .collect();
                if !fresh.is_empty() {
                    tracing::info!("learned {} correction(s) from {}", fresh.len(), snap.app);
                    let _ = events.send(Event::Corrections { dictation_id, app: snap.app.clone(), corrections: fresh }).await;
                }
            }
        }
    }
}

/// Text around the caret, sent to the server as dictation `context`.
fn field_context(snap: &FieldSnapshot) -> serde_json::Value {
    let chars: Vec<char> = snap.text.chars().collect();
    let caret = snap.caret.unwrap_or(chars.len()).min(chars.len());
    let before: String = chars[caret.saturating_sub(2000)..caret].iter().collect();
    let after: String = chars[caret..(caret + 500).min(chars.len())].iter().collect();
    serde_json::json!({ "app": snap.app, "before": before, "after": after })
}

fn is_short_press(pressed: Instant, released: Instant) -> bool {
    released.saturating_duration_since(pressed) < MIN_PRESS
}

fn dummy_capture() -> Capture {
    let (_tx, rx) = crossbeam_channel::bounded(0);
    Capture::new(rx, "none".into(), Box::new(()))
}

fn slugify(s: &str) -> String {
    s.chars().map(|c| if c.is_alphanumeric() { c.to_ascii_lowercase() } else { '-' }).collect::<String>().trim_matches('-').chars().take(40).collect()
}

mod chrono_lite {
    pub fn now_stamp() -> String {
        let secs = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
        // Civil-from-days (Howard Hinnant), enough for a filename.
        let days = (secs / 86400) as i64;
        let rem = secs % 86400;
        let z = days + 719468;
        let era = z.div_euclid(146097);
        let doe = z - era * 146097;
        let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
        let y = yoe + era * 400;
        let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
        let mp = (5 * doy + 2) / 153;
        let d = doy - (153 * mp + 2) / 5 + 1;
        let m = if mp < 10 { mp + 3 } else { mp - 9 };
        let y = if m <= 2 { y + 1 } else { y };
        format!("{y:04}{m:02}{d:02}-{:02}{:02}{:02}", rem / 3600, (rem % 3600) / 60, rem % 60)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn short_press_measured_from_key_down() {
        let down = Instant::now();
        assert!(is_short_press(down, down + Duration::from_millis(80)));
        // Held for 400 ms: kept, however long the connection took to open in between.
        assert!(!is_short_press(down, down + Duration::from_millis(400)));
        assert!(!is_short_press(down, down + MIN_PRESS));
        // Events out of order never count as long.
        assert!(is_short_press(down + Duration::from_millis(10), down));
    }
}
