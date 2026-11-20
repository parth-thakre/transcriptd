package dev.transcriptd.app

import android.Manifest
import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.InputMethod
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.content.pm.PackageManager
import android.content.Context
import android.graphics.Rect
import android.hardware.display.DisplayManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.text.InputType
import android.util.Log
import android.view.Display
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import android.view.inputmethod.EditorInfo
import android.widget.Toast
import org.json.JSONObject

/** Shows the bubble on every text box (whenever the keyboard is up or a text field has focus,
 *  on whichever screen you're using) and types the result at the caret. It only looks at the
 *  focused field. */
class FlowService : AccessibilityService(), Bubble.Callbacks, Dictation.Listener {

    private lateinit var prefs: Prefs
    private lateinit var bubble: Bubble
    private val main = Handler(Looper.getMainLooper())
    private val refresh = Runnable { refresh() }

    private var recorder: Recorder? = null
    private var dictation: Dictation? = null
    private var targetApp: String? = null
    private var targetField: AccessibilityNodeInfo? = null
    private var targetEditor: String? = null
    // The editor the keyboard is typing into, straight from Android (flagInputMethodEditor). It
    // works in apps whose text box isn't an editable node in the accessibility tree.
    private var editor: EditorInfo? = null
    private val blindReported = HashSet<String>()
    private val displayContexts = HashMap<Int, Context>()
    private var releasedAt = 0L
    private var report: Report? = null
    private var failed = false
    private var screen = "main"
    // The field the last dictation went into, watched until you're done with it (EditWatch).
    private var watched: Watched? = null
    private val watchPoll = object : Runnable {
        override fun run() {
            val w = watched ?: return
            sampleWatch()
            if (watched !== w) return
            w.logic.tick(SystemClock.uptimeMillis())?.let { deliver(w, it); return }
            main.postDelayed(this, WATCH_POLL_MS)
        }
    }

    override fun onServiceConnected() {
        prefs = Prefs(this)
        bubble = Bubble(this, this)
        refresh()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event != null && watched != null) watchEvent(event)
        // Typing and moving the cursor don't change whether there's a text box, only what's in it
        // (that's for the edit watch): re-placing the bubble on every keystroke made it twitch.
        val type = event?.eventType
        if (type == AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED || type == AccessibilityEvent.TYPE_VIEW_TEXT_SELECTION_CHANGED) return
        main.removeCallbacks(refresh)
        main.postDelayed(refresh, 90)
    }

    override fun onInterrupt() {}

    override fun onCreateInputMethod(): InputMethod = object : InputMethod(this) {
        override fun onStartInput(attribute: EditorInfo, restarting: Boolean) {
            super.onStartInput(attribute, restarting)
            // Every focused window starts input; only a real text field has an input type.
            editor = attribute.takeIf { it.inputType != InputType.TYPE_NULL }
            // Typing into another text box: you're done with the one the dictation went into.
            val key = editor?.let(::editorKey)
            main.post { watched?.editor?.let { if (key != null && key != it) endWatch(EditWatch.Reason.FOCUS_LOST) } }
            main.removeCallbacks(refresh); main.postDelayed(refresh, 90)
        }

        override fun onFinishInput() {
            super.onFinishInput()
            editor = null
            main.removeCallbacks(refresh); main.postDelayed(refresh, 90)
        }
    }

    override fun onDestroy() {
        main.removeCallbacks(watchPoll)
        watched = null
        stopRecording()
        dictation?.cancel()
        if (::bubble.isInitialized) bubble.remove()
        super.onDestroy()
    }

    // -- when to show it ------------------------------------------------------------------

    // The keyboard's top edge moves in steps while it opens (a first size, then its real one): the
    // bubble waits for it to hold still, so it makes one move instead of two lurches.
    private var lastFloor = Int.MIN_VALUE
    private var floorAt = 0L

    /** Shows or moves the bubble once the floor has been the same for SETTLE_MS. */
    private fun place(g: Bubble.Geometry) {
        val now = SystemClock.uptimeMillis()
        if (g.floor != lastFloor || g.display != lastDisplay) { lastFloor = g.floor; lastDisplay = g.display; floorAt = now }
        val wait = SETTLE_MS - (now - floorAt)
        if (wait > 0) { main.removeCallbacks(refresh); main.postDelayed(refresh, wait + 10); return }
        bubble.show(g)
    }
    private var lastDisplay = -1

    private fun refresh() {
        val (display, wins) = activeDisplay()
        val ime = wins.firstOrNull { it.type == AccessibilityWindowInfo.TYPE_INPUT_METHOD }
        val field = focusedField(wins)
        val ed = editor
        checkWatchFocus(wins, field)
        if (bubble.busy) { place(geometry(display, ime)); return }
        val password = field?.isPassword == true || (ed != null && isPassword(ed))
        // Every text box: the keyboard being up is enough, even when the box itself can't be seen
        // (web pages, custom editors); insert() types through the keyboard's own connection.
        if (!prefs.paired || password || (ime == null && field == null && ed == null)) { bubble.hide(); return }
        if (field == null && ed == null) reportBlind(wins)
        place(geometry(display, ime))
    }

    /** The screen you're using and its windows. Folded, the Flip's cover screen is a display of
     *  its own (id 1) and the main one is off, but `windows` only ever lists the main display's. */
    private fun activeDisplay(): Pair<Display, List<AccessibilityWindowInfo>> {
        val dm = getSystemService(DisplayManager::class.java)
        val all = try { windowsOnAllDisplays } catch (_: Exception) { null }
        fun wins(d: Display) = all?.get(d.displayId) ?: emptyList()
        val on = dm.displays.filter { it.state == Display.STATE_ON }
        val d = on.firstOrNull { d -> wins(d).any { it.type == AccessibilityWindowInfo.TYPE_INPUT_METHOD } }
            ?: on.firstOrNull { d -> wins(d).any { it.isFocused } }
            ?: on.firstOrNull() ?: dm.getDisplay(Display.DEFAULT_DISPLAY)
        return d to wins(d)
    }

    private fun current() = activeDisplay().second

    /** The focused editable node: in the focused window first, else in any other app or system
     *  window on that screen (the focused one isn't always the app you're typing in). */
    private fun focusedField(wins: List<AccessibilityWindowInfo> = current()): AccessibilityNodeInfo? = try {
        wins.asSequence()
            .filter { it.type == AccessibilityWindowInfo.TYPE_APPLICATION || it.type == AccessibilityWindowInfo.TYPE_SYSTEM }
            .sortedByDescending { it.isFocused }
            .mapNotNull { it.root?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT) }.firstOrNull { it.isEditable }
    } catch (_: Exception) { null }

    /** The app in front on that screen. */
    private fun frontApp(wins: List<AccessibilityWindowInfo> = current()): String? = try {
        wins.filter { it.type == AccessibilityWindowInfo.TYPE_APPLICATION }.sortedByDescending { it.isFocused }
            .firstNotNullOfOrNull { it.root?.packageName?.toString() }
    } catch (_: Exception) { null }

    private fun editorKey(e: EditorInfo) = "${e.packageName}:${e.fieldId}"

    private fun isPassword(e: EditorInfo): Boolean {
        val cls = e.inputType and InputType.TYPE_MASK_CLASS
        val v = e.inputType and InputType.TYPE_MASK_VARIATION
        return (cls == InputType.TYPE_CLASS_TEXT && v in setOf(InputType.TYPE_TEXT_VARIATION_PASSWORD,
            InputType.TYPE_TEXT_VARIATION_WEB_PASSWORD, InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD)) ||
            (cls == InputType.TYPE_CLASS_NUMBER && v == InputType.TYPE_NUMBER_VARIATION_PASSWORD)
    }

    /** The keyboard is up but the text box can't be seen: the bubble shows anyway. Tell the server
     *  once per app (what had focus, which windows were there), so insertion there can be checked. */
    private fun reportBlind(wins: List<AccessibilityWindowInfo>) {
        val app = frontApp(wins) ?: "?"
        if (app == packageName || !blindReported.add(app)) return
        val focus = try { wins.firstOrNull { it.isFocused }?.root?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT) } catch (_: Exception) { null }
        val names = try { wins.map { "${it.type}:${it.root?.packageName}" } } catch (_: Exception) { emptyList() }
        Report(this, prefs).apply {
            put("event", "bubble_blind")
            put("outcome", "shown: no text field seen")
            put("target_app", app)
            put("screen", screen)
            put("focus", focus?.let { JSONObject().put("class", it.className).put("editable", it.isEditable).put("focused", it.isFocused) })
            put("display_id", wins.firstOrNull()?.displayId)
            put("windows", org.json.JSONArray(names))
        }.send(this, prefs)
    }

    /** Screen size, keyboard top, and the Flip cover screen's camera band (the same geometry and
     *  test as the web app: a small, near-square screen; the Flip 7 is 1.07+ times taller than
     *  wide and its cameras take the bottom fifth from about 43% of the width to the right). */
    private fun geometry(display: Display, ime: AccessibilityWindowInfo?): Bubble.Geometry {
        val ctx = displayContexts.getOrPut(display.displayId) { createDisplayContext(display) }
        val bounds = ctx.getSystemService(WindowManager::class.java).currentWindowMetrics.bounds
        val density = ctx.resources.displayMetrics.density
        val w = bounds.width()
        val h = bounds.height()
        val wDp = w / density
        val hDp = h / density
        val aspect = h.toFloat() / w
        val cover = wDp <= 630 && hDp <= 700 && aspect in 0.75f..1.334f
        val band = when {
            !cover -> 0
            aspect >= 1.07f -> (h * 0.22f).toInt()
            else -> (h * 0.10f).toInt()
        }
        val imeTop = ime?.let { Rect().also { r -> it.getBoundsInScreen(r) }.top }
        val floor = minOf(imeTop ?: h, h - band)
        val screen = if (cover) "cover" else "main"
        this.screen = screen
        return Bubble.Geometry(display.displayId, w, h, floor, density, screen, prefs.bubbleRight(screen), prefs.bubbleLift(screen))
    }

    // -- bubble callbacks -----------------------------------------------------------------

    override fun onPress(): Boolean {
        if (dictation != null) return false
        ServerUrl.problem(prefs.server)?.let { toast(it); return false }
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            startActivity(Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            Toast.makeText(this, "Allow the microphone first", Toast.LENGTH_SHORT).show()
            return false
        }
        val wins = current()
        val field = focusedField(wins)
        targetField = field
        targetEditor = editor?.let(::editorKey)
        targetApp = field?.packageName?.toString() ?: editor?.packageName ?: frontApp(wins)
        val rep = Report(this, prefs)
        val style = Styles.forApp(prefs, targetApp)
        rep.put("screen", screen)
        rep.put("display_id", wins.firstOrNull()?.displayId)
        rep.put("field", if (field != null) "node" else if (targetEditor != null) "editor" else "keyboard only")
        rep.put("target_app", targetApp)
        rep.put("style", style)
        rep.put("path", "socket")
        if (targetApp == packageName && Trace.expected != null) rep.put("expected", Trace.expected)
        report = rep
        failed = false
        val d = Dictation(prefs, targetApp, surrounding(field), style, this, rep)
        dictation = d
        MicService.start(this)
        d.open()
        recorder = Recorder(this, prefs.micSource, rep, onFrame = { bytes, level ->
            d.frame(bytes)
            main.post { bubble.level(level) }
        }, onError = { msg ->
            main.post { if (dictation === d) { report?.put("error", msg); failSilently(); toast(msg) } }
        }).also { it.start() }
        bubble.set(Bubble.State.HOLD) // Bubble buzzes once it's clearly a hold, not a drag
        return true
    }

    override fun onFinish() {
        val d = dictation ?: return
        report?.put("mode", if (bubble.state == Bubble.State.LOCKED) "hands-free" else "hold")
        report?.mark("released")
        bubble.set(Bubble.State.BUSY)
        releasedAt = SystemClock.uptimeMillis()
        Thread { stopRecording(); d.end() }.start()
        Haptics.tick(this)
    }

    override fun onCancel() {
        val d = dictation ?: return
        dictation = null
        stopRecording()
        d.cancel()
        finishReport(if (failed) "not sent, discarded" else "cancelled")
        bubble.set(Bubble.State.IDLE)
        Haptics.tick(this)
        main.post(refresh)
    }

    override fun onRetry() {
        val d = dictation ?: return
        report?.put("retries", (report?.json()?.optInt("retries") ?: 0) + 1)
        bubble.set(Bubble.State.BUSY)
        d.retry()
    }

    override fun onDiscard() = onCancel()

    override fun onMoved(screen: String, right: Boolean, lift: Float) = prefs.saveBubble(screen, right, lift)

    // -- results --------------------------------------------------------------------------

    override fun onResult(result: JSONObject) {
        dictation = null
        val took = SystemClock.uptimeMillis() - releasedAt
        if (result.optBoolean("empty") || result.optString("text").isBlank()) {
            Trace.record(Trace.Entry(result, took, "nothing heard", report = finishReport("nothing heard", result, took)))
            bubble.set(Bubble.State.IDLE)
            Haptics.nothing(this)
            main.post(refresh)
            return
        }
        // insert_text is fitted to the caret (spacing, first-letter case) from the context we sent.
        val text = result.optString("insert_text").ifEmpty { result.optString("text") }
        // Only into the field you started in (node equality is window + view id, or the same app
        // and editor id; when neither could be seen, the same app still in front): not another
        // conversation's field, never a password field.
        val wins = current()
        val now = focusedField(wins)
        val ed = editor
        val same = when {
            targetField != null && now == targetField -> true
            targetEditor != null -> ed != null && editorKey(ed) == targetEditor
            targetField != null -> false
            else -> targetApp != null && frontApp(wins) == targetApp
        }
        val password = now?.isPassword == true || (ed != null && isPassword(ed))
        val via = if (same && !password) insert(text) else null
        if (via == null) {
            copy(text)
            toast("Copied. Paste it where you want it.")
        } else {
            (result.opt("dictation_id") as? Number)?.toInt()?.let { id ->
                startWatch(id, text, now, ed?.let(::editorKey), now?.packageName?.toString() ?: targetApp)
            }
        }
        Trace.record(Trace.Entry(result, took, via ?: "copied", report = finishReport(via ?: "copied", result, took)))
        bubble.set(Bubble.State.DONE)
        Haptics.done(this)
        main.postDelayed(refresh, 700)
    }

    override fun onFailure(message: String) {
        failed = true
        report?.mark("failed")
        report?.put("error", message)
        Trace.record(Trace.Entry(null, SystemClock.uptimeMillis() - releasedAt, "not sent", message, report?.json()))
        bubble.set(Bubble.State.ERROR)
        Haptics.nothing(this)
        toast("Couldn't reach transcriptd. Tap retry, or ✕ to discard.")
    }

    /** Closes this take's report and sends it (the server links it to the dictation). */
    private fun finishReport(outcome: String, result: JSONObject? = null, took: Long? = null): JSONObject? {
        val rep = report ?: return null
        report = null
        rep.mark("done")
        rep.put("outcome", outcome)
        if (took != null) rep.put("release_to_text_ms", took)
        result?.opt("dictation_id")?.let { if (it is Int) rep.put("dictation_id", it) }
        rep.send(this, prefs)
        return rep.json()
    }

    private fun failSilently() {
        finishReport("mic failed")
        dictation?.cancel()
        dictation = null
        stopRecording()
        bubble.set(Bubble.State.IDLE)
    }

    private fun stopRecording() {
        recorder?.stop()
        recorder = null
        MicService.stop(this)
    }

    // -- the text field -------------------------------------------------------------------

    /** Text before and after the caret, for spacing/case and as temporary vocabulary: at most
     *  CaretContext.BEFORE_MAX characters before and AFTER_MAX after, nothing else from the field. */
    private fun surrounding(field: AccessibilityNodeInfo?): JSONObject? {
        fun context(before: String, after: String) = JSONObject().put("app", targetApp ?: "")
            .put("before", CaretContext.before(before)).put("after", CaretContext.after(after))
        try {
            // One character more each way, so CaretContext can tell whether the edge cuts a word.
            val st = inputMethod?.currentInputConnection
                ?.getSurroundingText(CaretContext.BEFORE_MAX + 1, CaretContext.AFTER_MAX + 1, 0)
            if (st != null) {
                val t = st.text.toString()
                val s = st.selectionStart.coerceIn(0, t.length)
                val e = st.selectionEnd.coerceIn(s, t.length)
                return context(t.substring(0, s), t.substring(e))
            }
        } catch (_: Exception) {}
        val node = field ?: return null
        val t = if (node.isShowingHintText) "" else node.text?.toString() ?: ""
        val s = node.textSelectionStart.let { if (it < 0) t.length else it.coerceAtMost(t.length) }
        val e = node.textSelectionEnd.let { if (it < 0) s else it.coerceIn(s, t.length) }
        return context(t.substring(0, s), t.substring(e))
    }

    /** Types at the caret like a keyboard (accessibility input connection, Android 13+). If
     *  that is not available, rewrites the field's text around the selection, then pastes.
     *  Returns how it went in, or null if it didn't. */
    private fun insert(text: String): String? {
        try {
            val ic = inputMethod?.currentInputConnection
            if (ic != null) { ic.commitText(text, 1, null); return "typed in" }
        } catch (_: Exception) {}
        val node = focusedField() ?: return null
        val current = if (node.isShowingHintText) "" else node.text?.toString() ?: ""
        val s = node.textSelectionStart.let { if (it < 0) current.length else it.coerceAtMost(current.length) }
        val e = node.textSelectionEnd.let { if (it < 0) s else it.coerceIn(s, current.length) }
        val next = current.substring(0, s) + text + current.substring(e)
        val args = Bundle().apply { putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, next) }
        if (node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)) {
            val caret = s + text.length
            node.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, Bundle().apply {
                putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, caret)
                putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, caret)
            })
            return "set the field"
        }
        val cm = getSystemService(ClipboardManager::class.java)
        val saved = cm.primaryClip
        cm.setPrimaryClip(ClipData.newPlainText("transcriptd", text))
        val pasted = node.performAction(AccessibilityNodeInfo.ACTION_PASTE)
        if (pasted && saved != null) main.postDelayed({ cm.setPrimaryClip(saved) }, 600)
        return if (pasted) "pasted" else null
    }

    // -- what you did with it afterwards -------------------------------------------------

    /** The watched field: a node when the accessibility tree shows it (window + view id, to find
     *  it again if the app rebuilds it), else the keyboard's editor, read through the input
     *  connection. */
    private class Watched(val logic: EditWatch, var node: AccessibilityNodeInfo?, val window: Int, val viewId: String?, val editor: String?)

    private fun startWatch(id: Int, text: String, node: AccessibilityNodeInfo?, editorKey: String?, app: String?) {
        endWatch(EditWatch.Reason.REPLACED)
        if (!prefs.learnEdits) return // opt-in: off, the field isn't read again after the insertion
        if (node == null && editorKey == null) return // keyboard only: no way to tell it's the same field
        if (node?.isPassword == true || editor?.let(::isPassword) == true) return
        watched = Watched(EditWatch(id, text, app, SystemClock.uptimeMillis()), node, node?.windowId ?: -1, node?.viewIdResourceName, editorKey)
        // The insertion's own text-changed event usually comes first; this catches fields that send none.
        main.postDelayed(watchPoll, 250)
    }

    private fun sameField(w: Watched, n: AccessibilityNodeInfo): Boolean =
        (w.node != null && n == w.node) || (n.windowId == w.window && w.viewId != null && n.viewIdResourceName == w.viewId)

    private fun nodeText(n: AccessibilityNodeInfo) = if (n.isShowingHintText) "" else n.text?.toString() ?: ""

    /** The watched field's text now, or null when it can't be read (gone, or another editor). */
    private fun readField(w: Watched): String? {
        val n = w.node
        if (n != null) {
            if (n.refresh()) return nodeText(n)
            // The app rebuilt the view: the focused field with the same window and view id is it.
            val again = focusedField()?.takeIf { sameField(w, it) } ?: return null
            w.node = again
            return nodeText(again)
        }
        if (editor?.let(::editorKey) != w.editor) return null
        return try {
            inputMethod?.currentInputConnection?.getSurroundingText(WATCH_READ_MAX, WATCH_READ_MAX, 0)?.text?.toString()
        } catch (_: Exception) { null }
    }

    /** Text changes in the watched field. A sent message is usually cleared within the event
     *  throttle, so the event's text from before the change (the message) is read too. */
    private fun watchEvent(event: AccessibilityEvent) {
        val w = watched ?: return
        val type = event.eventType
        if (type != AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED && type != AccessibilityEvent.TYPE_VIEW_TEXT_SELECTION_CHANGED) return
        val src = try { event.source } catch (_: Exception) { null }
        if (src != null && !sameField(w, src)) return
        if (src == null && (w.node == null || event.windowId != w.window)) return
        if (event.isPassword || src?.isPassword == true) return
        if (type == AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED) {
            event.beforeText?.toString()?.takeIf { it.isNotEmpty() }?.let { w.logic.text(it) }
        }
        val now = src?.let(::nodeText) ?: readField(w) ?: return
        w.logic.text(now)?.let { deliver(w, it) }
    }

    /** One reading of the field; a field that's gone means you've moved on. */
    private fun sampleWatch() {
        val w = watched ?: return
        val t = readField(w)
        if (t == null) { if (w.node != null) endWatch(EditWatch.Reason.FOCUS_LOST, look = false); return }
        w.logic.text(t)?.let { deliver(w, it) }
    }

    /** Focus in another text box, or another app in front: done with the watched field. */
    private fun checkWatchFocus(wins: List<AccessibilityWindowInfo>, field: AccessibilityNodeInfo?) {
        val w = watched ?: return
        val other = field != null && w.node != null && !sameField(w, field)
        val front = frontApp(wins)
        if (other || (front != null && w.logic.app != null && front != w.logic.app)) endWatch(EditWatch.Reason.FOCUS_LOST)
    }

    /** Ends the watch (once), after a last look at the field unless it's known to be gone. */
    private fun endWatch(reason: EditWatch.Reason, look: Boolean = true) {
        val w = watched ?: return
        if (look) readField(w)?.let { t -> w.logic.text(t)?.let { deliver(w, it); return } }
        w.logic.finish(reason)?.let { deliver(w, it) }
    }

    /** Posts what became of the dictation so the server can learn from the edit: a window of the
     *  field around it (EditWatch.window) with what was inserted, or for "kept as is" just
     *  final_text null with the app and reason, no text. Fire and forget. */
    private fun deliver(w: Watched, out: EditWatch.Outcome) {
        if (watched === w) watched = null
        main.removeCallbacks(watchPoll)
        val id = w.logic.dictationId
        Log.i("transcriptd", "dictation $id: ${out.reason.wire}, changed=${out.changed}, send=${out.send}")
        Trace.edited(id, out)
        if (!out.send || !prefs.learnEdits) return
        // Unchanged: no text at all, just the signal (the server has its own copy of what went in).
        val body = JSONObject().put("final_text", out.finalText ?: JSONObject.NULL)
            .put("app", w.logic.app ?: JSONObject.NULL).put("reason", out.reason.wire)
        if (out.changed) body.put("inserted_text", w.logic.inserted)
        Thread {
            try { Api.edited(prefs, id, body) } catch (e: Exception) { Log.w("transcriptd", "edit for dictation $id not sent: ${e.message}") }
        }.start()
    }

    private fun copy(text: String) =
        getSystemService(ClipboardManager::class.java).setPrimaryClip(ClipData.newPlainText("transcriptd", text))

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()

    companion object {
        private const val WATCH_POLL_MS = 1000L
        private const val SETTLE_MS = 260L
        private const val WATCH_READ_MAX = 10_000
    }
}
