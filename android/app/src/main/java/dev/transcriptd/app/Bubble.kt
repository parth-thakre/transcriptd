package dev.transcriptd.app

import android.accessibilityservice.AccessibilityService
import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PixelFormat
import android.graphics.RectF
import android.hardware.display.DisplayManager
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.view.Display
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import kotlin.math.PI
import kotlin.math.hypot
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sin

/** The floating dictation bubble, drawn as an accessibility overlay over whatever app has a
 *  text field focused.
 *
 *  Press: recording starts at once (nothing said is lost). Release after a moment: done,
 *  the text goes in. Release quickly (a tap): it keeps listening hands-free and the bubble
 *  becomes a small bar with ✕ and ✓. Slide off while holding: discard. Drag (move before the
 *  hold registers): reposition; it snaps to the nearest side and remembers the spot, separately
 *  for the cover screen and the main screen. */
class Bubble(private val service: AccessibilityService, private val cb: Callbacks) {

    interface Callbacks {
        fun onPress(): Boolean // start recording; false if it could not start
        fun onFinish()
        fun onCancel()
        fun onRetry()
        fun onDiscard()
        fun onMoved(screen: String, right: Boolean, lift: Float)
    }

    enum class State { IDLE, HOLD, CANCEL, LOCKED, BUSY, DONE, ERROR }

    /** [floor]: the bubble stays above this y (the keyboard's top edge, or on the cover screen
     *  the camera band when no keyboard is up). [lift] and [right] are the remembered spot. */
    data class Geometry(val display: Int, val width: Int, val height: Int, val floor: Int, val density: Float, val screen: String,
                        val right: Boolean, val lift: Float)

    // Its window lives on one display at a time; the Flip's cover screen is a display of its own.
    private var display = Display.DEFAULT_DISPLAY
    private var wm = service.getSystemService(WindowManager::class.java)
    private val main = Handler(Looper.getMainLooper())
    private var view = BubbleView(service)
    private val lp = WindowManager.LayoutParams(
        WindowManager.LayoutParams.WRAP_CONTENT, WindowManager.LayoutParams.WRAP_CONTENT,
        WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
        WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or
            WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
        PixelFormat.TRANSLUCENT,
    ).apply { gravity = Gravity.TOP or Gravity.START }

    private var geo: Geometry? = null
    private var shown = false
    var state = State.IDLE
        private set
    /** Recording, sending or showing an error: keep it on screen even if focus moves. */
    val busy get() = state != State.IDLE && state != State.DONE

    private var level = 0f
    private val levels = FloatArray(16)
    private var levelAt = 0
    // Voice level in dB against this take's own room and voice (the last 2 s), so a quiet
    // voice on a quiet mic still fills the range. Tuned on real phone takes.
    private val history = FloatArray(100)
    private var heard = 0
    private val ripples = LongArray(3) // start times of the rings sent out at each syllable onset
    private var rippleAt = 0
    private var lastRipple = 0L

    fun set(next: State) {
        state = next
        if (next == State.DONE) main.postDelayed({ if (state == State.DONE) set(State.IDLE) }, 650)
        if (next != State.HOLD && next != State.LOCKED && next != State.CANCEL) {
            level = 0f; levels.fill(0f); ripples.fill(0L); heard = 0
        }
        relayout()
        view.invalidate()
    }

    /** A 20 ms frame's peak (0-1). */
    fun level(l: Float) {
        val db = 20f * log10(max(l, 1e-4f))
        if (db <= -79f) return // the mic's first frames are exact zeros
        history[heard++ % history.size] = db
        val n = min(heard, history.size)
        val recent = FloatArray(min(n, 75)) { history[(heard - 1 - it) % history.size] }.apply { sort() }
        val floorDb = recent[(recent.size * 0.15f).toInt()]              // the room: the quiet end of the last 1.5 s
        var ceilDb = floorDb + 9f                                       // your voice: the loudest of the last 2 s
        for (i in 0 until n) ceilDb = max(ceilDb, history[i])
        val target = ((db - floorDb - 2f) / (ceilDb - floorDb - 2f)).coerceIn(0f, 1f)
        val was = level
        level += (target - level) * (if (target > level) 0.7f else 0.2f) // quick on, slower off: a beat per syllable
        val now = SystemClock.uptimeMillis()
        if (level > 0.4f && was < 0.3f && now - lastRipple > 160) { ripples[rippleAt++ % ripples.size] = now; lastRipple = now }
        levels[levelAt++ % levels.size] = level
        view.invalidate()
    }

    fun show(g: Geometry) {
        if (g.display != display) {
            if (busy) return // mid-take: stay where your finger is
            if (!moveTo(g.display)) return
        }
        geo = g
        relayout()
        if (!shown) {
            view.alpha = 0f
            wm.addView(view, lp)
            shown = true
            view.animate().alpha(1f).setDuration(140).start()
        }
    }

    fun hide() {
        if (!shown || busy) return
        shown = false
        wm.removeView(view)
    }

    private fun moveTo(id: Int): Boolean {
        val d = service.getSystemService(DisplayManager::class.java).getDisplay(id) ?: return false
        if (shown) { wm.removeView(view); shown = false }
        // An accessibility service's display context carries its overlay token for that display.
        val ctx = service.createDisplayContext(d)
        wm = ctx.getSystemService(WindowManager::class.java)
        view = BubbleView(ctx)
        display = id
        return true
    }

    fun remove() {
        if (shown) wm.removeView(view)
        shown = false
    }

    // -- layout ---------------------------------------------------------------------------

    private val d get() = geo?.density ?: service.resources.displayMetrics.density
    private val cover get() = geo?.screen == "cover"
    private val size get() = (if (cover) 44 else 50) * d
    private val edge get() = 6 * d
    /** Room for the voice-level halo, only while held, so the idle bubble blocks nothing
     *  around it. The window grows around the same centre. */
    private val halo get() = if (look == State.HOLD || look == State.CANCEL) 12 * d else 0f

    // Right after the finger goes down it might be a drag: recording starts at once (nothing said
    // is lost), the bubble follows the finger, and it only turns into "listening" once it's
    // clearly a hold (still for QUIET_MS), so moving it never flashes red or buzzes.
    private var quiet = false
    private val look get() = if (quiet && state == State.HOLD) State.IDLE else state
    private var following = false
    private val unquiet = Runnable {
        if (quiet) {
            quiet = false; following = false // it's a hold: settle back where it was
            if (state == State.HOLD) Haptics.start(service)
            relayout(); view.invalidate()
        }
    }
    private val pill get() = state == State.LOCKED || state == State.ERROR
    private val boxH get() = (size + 2 * halo).toInt()
    private val boxW get() = if (pill) (size * 3.3f).toInt() else boxH

    private var glide: android.animation.ValueAnimator? = null
    private var glideTo: Pair<Int, Int>? = null

    private fun relayout() {
        val g = geo ?: return
        val oldW = lp.width; val oldH = lp.height
        val resized = oldW != boxW || oldH != boxH
        if (dragging || following) {
            // The finger places it. A size change (the halo going) keeps the centre where it is.
            if (resized) {
                lp.x += (oldW - boxW) / 2; lp.y += (oldH - boxH) / 2
                lp.width = boxW; lp.height = boxH
                if (shown) wm.updateViewLayout(view, lp)
            }
            return
        }
        lp.width = boxW; lp.height = boxH
        val h = halo.toInt()
        val tx = if (g.right) (g.width - edge - size - h - (boxW - boxH)).toInt() else (edge - h).toInt()
        // With the keyboard up the cover screen has ~120 dp left: there, covering status-bar
        // icons beats sliding down onto the app's send or voice button. 12 dp keeps the hold
        // halo on screen.
        val top = ((if (cover) 12 else 28) * d).toInt()
        val ty = (g.floor - g.lift * d - size).toInt().coerceIn(top, max(top, g.floor - size.toInt())) - h
        if (tx == lp.x && ty == lp.y && !resized) return // nothing moved: leave the window alone
        if (!shown || resized || oldW <= 0) {
            // First showing, or the bubble grew/shrank around its centre: no travel to animate.
            glide?.cancel(); glideTo = null
            lp.x = tx; lp.y = ty
            if (shown) wm.updateViewLayout(view, lp)
            return
        }
        // It has somewhere to go (the keyboard came up, or it snapped to a side): glide there.
        if (glideTo == tx to ty && glide?.isRunning == true) return
        glide?.cancel()
        val fx = lp.x; val fy = lp.y
        glideTo = tx to ty
        glide = android.animation.ValueAnimator.ofFloat(0f, 1f).apply {
            duration = 200; interpolator = android.view.animation.DecelerateInterpolator(1.8f)
            addUpdateListener { a ->
                val f = a.animatedValue as Float
                lp.x = (fx + (tx - fx) * f).toInt(); lp.y = (fy + (ty - fy) * f).toInt()
                if (shown) try { wm.updateViewLayout(view, lp) } catch (_: Exception) { cancel() }
            }
            start()
        }
    }

    // -- touch ----------------------------------------------------------------------------

    private var dragging = false
    private var downX = 0f
    private var downY = 0f
    // Moves are applied once per frame: a finger sends more than the window manager can take, and
    // the backlog made the bubble stall and then jump.
    private var dragFrame = false
    private val applyDrag = android.view.Choreographer.FrameCallback {
        dragFrame = false
        if (shown && (dragging || following)) try { wm.updateViewLayout(view, lp) } catch (_: Exception) {}
    }

    // The window's centre when the finger went down: a drag moves the centre, so a size change
    // under the finger (the halo appearing or going) never makes it jump.
    private var anchorX = 0f
    private var anchorY = 0f
    private var downAt = 0L
    private var pressed = false

    @SuppressLint("ClickableViewAccessibility")
    private fun touch(e: MotionEvent): Boolean {
        if (pill) {
            if (e.actionMasked == MotionEvent.ACTION_UP) {
                val left = e.x < view.width / 2f
                if (state == State.LOCKED) { if (left) cb.onCancel() else cb.onFinish() }
                else { if (left) cb.onDiscard() else cb.onRetry() }
            }
            return true
        }
        if (state == State.BUSY || state == State.DONE) return true
        val slop = android.view.ViewConfiguration.get(view.context).scaledTouchSlop.toFloat()
        when (e.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                glide?.cancel(); glideTo = null
                quiet = true; main.removeCallbacks(unquiet); main.postDelayed(unquiet, QUIET_MS)
                downX = e.rawX; downY = e.rawY
                anchorX = lp.x + lp.width / 2f; anchorY = lp.y + lp.height / 2f
                downAt = SystemClock.uptimeMillis()
                dragging = false; following = false
                pressed = cb.onPress()
            }
            MotionEvent.ACTION_MOVE -> {
                val dist = hypot(e.rawX - downX, e.rawY - downY)
                if (quiet && !dragging) {
                    // Undecided: it follows the finger from the first move, so a drag never has to
                    // "catch" first. Past the touch slop it's a drag, not dictation.
                    following = true
                    if (dist > slop) {
                        dragging = true
                        main.removeCallbacks(unquiet)
                        if (pressed) { pressed = false; cb.onCancel() }
                        quiet = false
                    }
                }
                if (dragging || following) {
                    lp.x = (anchorX + e.rawX - downX - lp.width / 2f).toInt()
                    lp.y = (anchorY + e.rawY - downY - lp.height / 2f).toInt()
                    if (!dragFrame) { dragFrame = true; android.view.Choreographer.getInstance().postFrameCallback(applyDrag) }
                } else if (pressed) {
                    val far = dist > CANCEL_DP * d
                    if (far != (state == State.CANCEL)) { set(if (far) State.CANCEL else State.HOLD); Haptics.tick(service) }
                }
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                // Let go before it was clearly a hold: show what's happening now (a tap locks it on).
                main.removeCallbacks(unquiet); quiet = false
                if (following && !dragging) { following = false; relayout() } // a tap that wobbled: back in place
                following = false
                if (dragging) {
                    android.view.Choreographer.getInstance().removeFrameCallback(applyDrag); dragFrame = false
                    dragging = false
                    val g = geo ?: return true
                    val right = lp.x + boxW / 2 > g.width / 2
                    val lift = ((g.floor - lp.y - halo - size) / d).coerceAtLeast(0f)
                    geo = g.copy(right = right, lift = lift)
                    cb.onMoved(g.screen, right, lift)
                    relayout()
                } else if (pressed) {
                    pressed = false
                    when {
                        // The system took the gesture (a call, the shade): discard, never submit.
                        e.actionMasked == MotionEvent.ACTION_CANCEL || state == State.CANCEL -> cb.onCancel()
                        SystemClock.uptimeMillis() - downAt < HOLD_MS -> { set(State.LOCKED); Haptics.tick(service) }
                        else -> cb.onFinish()
                    }
                }
            }
        }
        return true
    }

    // -- drawing --------------------------------------------------------------------------

    @SuppressLint("ViewConstructor")
    private inner class BubbleView(context: Context) : View(context) {
        private val fill = Paint(Paint.ANTI_ALIAS_FLAG)
        private val line = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeCap = Paint.Cap.ROUND; strokeJoin = Paint.Join.ROUND }
        private val text = Paint(Paint.ANTI_ALIAS_FLAG).apply { textAlign = Paint.Align.CENTER }
        private val path = Path()
        private val rect = RectF()

        init { setOnTouchListener { _, e -> touch(e) } }

        override fun onDraw(c: Canvas) {
            val cy = height / 2f
            val r = size / 2
            if (pill) return drawPill(c, cy, r)
            val cx = width / 2f
            when (look) {
                State.HOLD -> {
                    val now = SystemClock.uptimeMillis()
                    val room = halo - 1 * d
                    // Always breathing, so it's clearly listening in a pause; the voice swells it.
                    val breath = 0.5f + 0.5f * sin(now / 900f * 2f * PI.toFloat())
                    val glow = room * (0.18f + 0.12f * breath + 0.7f * level)
                    fill.color = 0x40FF6358; c.drawCircle(cx, cy, r + glow, fill)
                    fill.color = 0x30FF6358; c.drawCircle(cx, cy, r + glow * 0.55f, fill)
                    for (t in ripples) {
                        val age = (now - t) / 520f
                        if (t == 0L || age >= 1f) continue
                        line.color = REC; line.strokeWidth = 2 * d; line.alpha = ((1f - age) * 200).toInt()
                        c.drawCircle(cx, cy, r + room * age, line)
                        line.alpha = 255
                    }
                    fill.color = REC; c.drawCircle(cx, cy, r * (1 + 0.04f * level), fill)
                    mic(c, cx, cy, r, 0xFF2A0705.toInt())
                    postInvalidateOnAnimation()
                }
                State.CANCEL -> {
                    fill.color = 0xFF2A2F35.toInt(); c.drawCircle(cx, cy, r, fill)
                    cross(c, cx, cy, r * 0.32f, MUTED)
                }
                State.BUSY -> {
                    fill.color = 0xF2111316.toInt(); c.drawCircle(cx, cy, r, fill)
                    mic(c, cx, cy, r, MINT and 0x99FFFFFF.toInt())
                    line.color = MINT; line.strokeWidth = 3 * d
                    val a = (SystemClock.uptimeMillis() % 900) / 900f * 360f
                    rect.set(cx - r + 1.5f * d, cy - r + 1.5f * d, cx + r - 1.5f * d, cy + r - 1.5f * d)
                    c.drawArc(rect, a, 110f, false, line)
                    postInvalidateOnAnimation()
                }
                State.DONE -> {
                    fill.color = OK; c.drawCircle(cx, cy, r, fill)
                    check(c, cx, cy, r * 0.34f, 0xFF06231A.toInt())
                }
                else -> {
                    fill.color = 0xF0194239.toInt(); c.drawCircle(cx, cy, r, fill)
                    line.color = 0xFF2E6B5C.toInt(); line.strokeWidth = 1.5f * d
                    c.drawCircle(cx, cy, r - 0.75f * d, line)
                    mic(c, cx, cy, r, MINT)
                }
            }
        }

        /** Hands-free (✕ · level · ✓) or not sent (✕ · "Not sent" · retry). */
        private fun drawPill(c: Canvas, cy: Float, r: Float) {
            val left = 0f
            val right = width.toFloat()
            rect.set(left, cy - r, right, cy + r)
            fill.color = 0xF2111316.toInt(); c.drawRoundRect(rect, r, r, fill)
            line.color = 0xFF2A2F35.toInt(); line.strokeWidth = 1 * d
            c.drawRoundRect(rect, r, r, line)
            val br = r - 5 * d
            // ✕ on the left
            fill.color = 0xFF1F2328.toInt(); c.drawCircle(left + r, cy, br, fill)
            cross(c, left + r, cy, br * 0.38f, 0xFFC4C9CF.toInt())
            // action on the right
            val ax = right - r
            if (state == State.LOCKED) {
                fill.color = MINT; c.drawCircle(ax, cy, br, fill)
                check(c, ax, cy, br * 0.42f, 0xFF0B1512.toInt())
                // level bars
                val n = 7
                val span = (ax - br) - (left + r + br) - 12 * d
                val x0 = left + r + br + 6 * d
                val step = span / n
                fill.color = REC
                for (i in 0 until n) {
                    val v = levels[(levelAt - n + i + levels.size * 4) % levels.size]
                    val idle = 0.12f + 0.08f * sin(SystemClock.uptimeMillis() / 260f + i * 0.9f) // a slow wave in silence
                    val h = max(3 * d, max(v, idle) * r * 1.3f)
                    rect.set(x0 + i * step + step * 0.3f, cy - h / 2, x0 + i * step + step * 0.7f, cy + h / 2)
                    c.drawRoundRect(rect, 2 * d, 2 * d, fill)
                }
            } else {
                fill.color = WARN; c.drawCircle(ax, cy, br, fill)
                retry(c, ax, cy, br * 0.42f, 0xFF231705.toInt())
                text.color = 0xFFC4C9CF.toInt(); text.textSize = 12.5f * d
                c.drawText("Not sent", (left + r + ax) / 2 + 2 * d, cy + 4.5f * d, text)
            }
        }

        private fun mic(c: Canvas, cx: Float, cy: Float, r: Float, color: Int) {
            val u = r * 0.058f // glyph unit, drawn on a 24-unit grid like the web icons
            line.color = color; line.strokeWidth = 2.1f * u
            rect.set(cx - 3 * u, cy - 9 * u, cx + 3 * u, cy + 2 * u)
            c.drawRoundRect(rect, 3 * u, 3 * u, line)
            path.reset()
            path.moveTo(cx - 7 * u, cy - 1 * u)
            path.cubicTo(cx - 7 * u, cy + 7 * u, cx + 7 * u, cy + 7 * u, cx + 7 * u, cy - 1 * u)
            path.moveTo(cx, cy + 6 * u); path.lineTo(cx, cy + 9 * u)
            c.drawPath(path, line)
        }

        private fun cross(c: Canvas, cx: Float, cy: Float, s: Float, color: Int) {
            line.color = color; line.strokeWidth = 2.2f * d
            c.drawLine(cx - s, cy - s, cx + s, cy + s, line)
            c.drawLine(cx + s, cy - s, cx - s, cy + s, line)
        }

        private fun check(c: Canvas, cx: Float, cy: Float, s: Float, color: Int) {
            line.color = color; line.strokeWidth = 2.6f * d
            path.reset()
            path.moveTo(cx - s, cy + s * 0.05f); path.lineTo(cx - s * 0.3f, cy + s * 0.75f); path.lineTo(cx + s, cy - s * 0.65f)
            c.drawPath(path, line)
        }

        private fun retry(c: Canvas, cx: Float, cy: Float, s: Float, color: Int) {
            line.color = color; line.strokeWidth = 2.4f * d
            rect.set(cx - s, cy - s, cx + s, cy + s)
            c.drawArc(rect, -60f, 290f, false, line)
            path.reset()
            path.moveTo(cx + s * 0.5f, cy - s * 1.15f); path.lineTo(cx + s * 0.55f, cy - s * 0.35f); path.lineTo(cx + s * 1.25f, cy - s * 0.6f)
            c.drawPath(path, line)
        }
    }

    companion object {
        private const val HOLD_MS = 320L
        private const val QUIET_MS = 250L // how long a press stays undecided between drag and hold
        private const val CANCEL_DP = 90
        private const val REC = 0xFFFF6358.toInt()
        private const val MINT = 0xFFE9F7F2.toInt()
        private const val OK = 0xFF4FCC9E.toInt()
        private const val WARN = 0xFFEFB04C.toInt()
        private const val MUTED = 0xFF8B929B.toInt()
    }
}
