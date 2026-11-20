package dev.transcriptd.app

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.ClipboardManager
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.window.OnBackInvokedDispatcher
import android.window.OnBackInvokedCallback
import android.view.inputmethod.InputMethodManager
import android.media.MediaRecorder
import android.os.Bundle
import android.provider.Settings
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.WindowInsets
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView
import org.json.JSONObject

/** Setup: pair with the server, grant the microphone and the bubble, pick a style, try it.
 *  Plain views, laid out for the Flip cover screen too (bottom padding clears the cameras). */
class MainActivity : Activity() {

    private lateinit var prefs: Prefs
    private lateinit var root: LinearLayout
    private var health: JSONObject? = null
    private var healthError: String? = null
    // The playground: a test field, and under it what happened to the last dictation (Trace).
    private var playground = false
    private var backToSetup: OnBackInvokedCallback? = null
    private var scroll: ScrollView? = null
    private var tryField: EditText? = null
    private var traceCard: LinearLayout? = null
    // Takes read from a known sentence are scored by the server's telemetry (Trace.expected).
    private var reading = true
    private var sentence = 0
    private var resumed = false
    // Views go into the open card (card { ... }), else straight into the page.
    private var into: LinearLayout? = null
    private val host: LinearLayout get() = into ?: root
    private var editingServer = false
    // Motion happens once per change: the caption that's showing, the style just picked, the take
    // just shown in the playground.
    private var shownHeadline: String? = null
    private var justPicked: String? = null
    private var revealedTrace: Any? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        intent?.data?.let { confirmPair(it.toString()); intent.data = null }
        build()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        intent.data?.let { confirmPair(it.toString()) }
        build()
    }

    override fun onResume() {
        super.onResume()
        Trace.listener = { if (playground) onTrace() }
        resumed = true
        syncExpected()
        build()
        check()
    }

    override fun onPause() {
        Trace.listener = null
        resumed = false
        syncExpected()
        super.onPause()
    }

    private fun openPlayground(on: Boolean) {
        playground = on; toTop = true
        backToSetup?.let { onBackInvokedDispatcher.unregisterOnBackInvokedCallback(it) }
        backToSetup = if (on) OnBackInvokedCallback { openPlayground(false) }.also {
            onBackInvokedDispatcher.registerOnBackInvokedCallback(OnBackInvokedDispatcher.PRIORITY_DEFAULT, it)
        } else null
        syncExpected()
        build()
    }

    private fun syncExpected() {
        Trace.expected = if (playground && reading && resumed) SENTENCES[sentence % SENTENCES.size] else null
    }

    /** A result came in: drop the keyboard (on the cover screen it hides most of the page) and
     *  show what happened. */
    private fun onTrace() {
        tryField?.let { getSystemService(InputMethodManager::class.java).hideSoftInputFromWindow(it.windowToken, 0) }
        renderTrace()
        scroll?.post { traceCard?.let { c -> scroll?.smoothScrollTo(0, c.top - px(8)) } }
    }

    // -- pairing --------------------------------------------------------------------------

    /** A pairing link opened from outside (any app or web page can open one): ask before
     *  sending dictations somewhere new. */
    private fun confirmPair(link: String) {
        val (server, token) = parsePair(link) ?: return
        if (server == prefs.server && token == prefs.token) return
        ServerUrl.problem(server)?.let { note(it); return }
        AlertDialog.Builder(this)
            .setTitle("Pair with this server?")
            .setMessage("$server\n\nYour dictations (audio and the text around the cursor) will go there.")
            .setPositiveButton("Pair") { _, _ -> prefs.server = server; prefs.token = token; health = null; healthError = null; build(); check() }
            .setNegativeButton("Cancel", null)
            .show()
    }

    /** True when [link] was a pairing link: used, or refused with a note saying why. */
    private fun pairFrom(link: String): Boolean {
        val (server, token) = parsePair(link) ?: return false
        ServerUrl.problem(server)?.let { note(it); return true }
        prefs.server = server; prefs.token = token
        return true
    }

    /** transcriptd://pair?u=<server>&t=<token>, or the phone web app's https://host/m/#t=<token>. */
    private fun parsePair(link: String): Pair<String, String>? {
        val uri = Uri.parse(link.trim())
        if (uri.scheme == "transcriptd") {
            val u = uri.getQueryParameter("u") ?: return null
            val t = uri.getQueryParameter("t") ?: return null
            return u.trim().trimEnd('/') to t.trim()
        }
        if (uri.scheme == "http" || uri.scheme == "https") {
            val t = uri.fragment?.removePrefix("t=")?.takeIf { uri.fragment!!.startsWith("t=") } ?: return null
            return "${uri.scheme}://${uri.encodedAuthority}" to t
        }
        return null
    }

    private fun check() {
        if (!prefs.paired) return
        Thread {
            val (h, err) = try { Api.health(prefs) to null } catch (e: Exception) { null to (e.message ?: "unreachable") }
            // Don't rebuild under someone typing in the try-it field.
            runOnUiThread {
                health = h; healthError = err
                val stt = sttProfiles(h)
                if (stt.isNotEmpty() && prefs.profile !in stt) prefs.profile = stt[0]
                if (currentFocus !is EditText) build()
            }
        }.start()
    }

    /** Speech-to-text profiles (never chat_audio): Whisper first, and of those a fixed-English
     *  one first, the most predictable for dictation. */
    private fun sttProfiles(h: JSONObject?): List<String> {
        val p = h?.optJSONObject("profiles") ?: return emptyList()
        val names = p.keys().asSequence().filter { p.getJSONObject(it).optString("engine") != "chat_audio" }.toList()
        val whisper = { n: String -> "whisper" in p.getJSONObject(n).optString("model").lowercase() }
        return names.sortedWith(compareBy({ !whisper(it) }, { p.getJSONObject(it).optString("language") != "en" }))
    }

    // -- state ----------------------------------------------------------------------------

    private val micGranted get() = checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
    private val notifyGranted get() = checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
    private val bubbleOn: Boolean get() {
        val enabled = Settings.Secure.getString(contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES) ?: return false
        val me = ComponentName(this, FlowService::class.java)
        return enabled.split(':').any { ComponentName.unflattenFromString(it) == me }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) = build()

    // -- UI -------------------------------------------------------------------------------

    private val d get() = resources.displayMetrics.density
    private fun px(v: Int) = (v * d).toInt()

    private var toTop = false

    private fun build() {
        // Rebuilding after a choice keeps your place on the page; switching screens starts at the top.
        val keepY = if (toTop) 0 else this.scroll?.scrollY ?: 0
        toTop = false
        val scroll = ScrollView(this).apply { isFillViewport = true; setBackgroundColor(BG) }
        this.scroll = scroll
        root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        scroll.addView(root)
        // Edge to edge (Android 15+): the viewport stops at the status and navigation bars, and on
        // the cover screen above the cameras, so nothing rests or scrolls under them.
        scroll.clipToPadding = true
        scroll.isVerticalFadingEdgeEnabled = true
        scroll.setFadingEdgeLength(px(20))
        scroll.setOnApplyWindowInsetsListener { _, insets ->
            val bars = insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout())
            val ime = insets.getInsets(WindowInsets.Type.ime()).bottom
            scroll.setPadding(0, bars.top, 0, maxOf(bars.bottom, cameraBand(), ime))
            root.setPadding(px(16) + bars.left, px(14), px(16) + bars.right, px(24))
            insets
        }
        if (playground) buildPlayground() else buildSetup()
        setContentView(scroll)
        if (keepY > 0) scroll.post { scroll.scrollTo(0, keepY) }
    }

    private fun styleOptions() = listOf("app" to "By app", "auto" to "Plain") +
        (health?.optJSONArray("styles")?.let { a -> (0 until a.length()).map { a.getJSONObject(it) }
            .filter { it.optString("name") != "auto" }.map { it.optString("name") to it.optString("label") } }
            ?: listOf("message" to "Message", "note" to "Note", "email" to "Email"))

    private fun buildSetup() {
        val missing = listOfNotNull("microphone".takeIf { !micGranted }, "bubble".takeIf { !bubbleOn },
            "notice".takeIf { !notifyGranted })
        val (headline, sub) = when {
            !prefs.paired -> "Connect your server." to "Paste the pairing link from your server, or type its address."
            ServerUrl.problem(prefs.server) != null -> "Use https for this server." to (ServerUrl.problem(prefs.server) ?: "")
            healthError != null -> "Can't reach your server." to (healthError ?: "")
            health == null -> "Checking your server…" to prefs.server
            missing.isNotEmpty() -> "Almost ready." to
                "Still to do: ${missing.joinToString(" and ").replace("microphone and bubble and", "microphone, bubble and")}."
            else -> "Ready when you are." to "Hold the bubble in any text box and talk. Tap it to talk hands-free."
        }
        headline(headline, sub, alarm = prefs.paired && healthError != null)

        // What's missing, and only that.
        if (missing.isNotEmpty() && prefs.paired && health != null) {
            gap(20)
            if (!micGranted) todo("Microphone", "So it can hear you.", "Allow") {
                requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 1)
            }
            if (!bubbleOn) {
                todo("Dictation bubble", "Shows up over text boxes in every app.", "Turn on") {
                    startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
                    note("Installed apps › transcriptd dictation bubble › On")
                }
                // Android blocks accessibility for apps installed from a file until you allow it.
                small("If Android says the app was denied access: App info › ⋮ › Allow restricted settings, then turn it on again.", indent = 17)
                textButton("Open App info", indent = 17) {
                    startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName")))
                }
            }
            if (!notifyGranted) todo("Listening notice", "Shows while the microphone is on.", "Allow") {
                requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 2)
            }
        }

        // Server
        heading("Server")
        if (prefs.paired && health != null && !editingServer) {
            line(prefs.server.removePrefix("http://").removePrefix("https://"), "Change") { editingServer = true; build() }
        } else {
            val server = field(prefs.server, "https://your-server.example.ts.net", InputType.TYPE_TEXT_VARIATION_URI)
            val token = field(prefs.token, "Token, not needed on your own tailnet", InputType.TYPE_TEXT_VARIATION_PASSWORD)
            row(
                button("Paste pairing link") {
                    val clip = getSystemService(ClipboardManager::class.java).primaryClip?.getItemAt(0)?.coerceToText(this)?.toString() ?: ""
                    if (pairFrom(clip)) { editingServer = false; health = null; healthError = null; build(); check() }
                    else note("Copy the link `transcriptd mobile` prints, then tap this.")
                },
                button("Save", primary = true) {
                    // Plain http:// only on your own network (ServerUrl); say so instead of saving.
                    ServerUrl.problem(server.text.toString())?.let { note(it); return@button }
                    prefs.server = server.text.toString(); prefs.token = token.text.toString()
                    editingServer = false; health = null; healthError = null; build(); check()
                },
            )
        }

        // How it comes out: the same thing said, through each style.
        heading("How it comes out")
        host.addView(TextView(this).apply {
            text = "“$SPOKEN”"; textSize = 15f; setTextColor(DUST); typeface = face(400)
            setLineSpacing(0f, 1.25f); setPadding(0, 0, 0, px(10))
        })
        for ((value, label) in styleOptions()) {
            specimen(label, SAMPLES[value], value == prefs.style, bigSample = value != "app",
                reveal = value == justPicked) { justPicked = value; prefs.style = value; build() }
        }
        gap(6)
        switchRow("Polish with the LLM", when {
            health != null && !health!!.optBoolean("llm_configured") -> "Your server has no LLM set up, so this does nothing."
            prefs.llm -> "Punctuation, capitals, numbers, and the Ramble rewrite. Checked against what you said."
            else -> "Off: the speech model's own punctuation and the rule-based cleanup only."
        }, prefs.llm) { prefs.llm = it; build() }
        learnSwitch()

        // Speech model
        val profiles = health?.optJSONObject("profiles")
        val stt = sttProfiles(health)
        if (profiles != null && stt.isNotEmpty()) {
            heading("Speech model")
            for (name in stt) {
                val model = profiles.optJSONObject(name)?.optString("model")?.substringAfterLast('/')?.removeSuffix("-gguf")
                specimen(name, model, name == prefs.profile, bigSample = false) { prefs.profile = name; build() }
            }
        }

        heading("Try it")
        small("Dictate into a test field and see what was heard, what changed, and how long it took.")
        gap(8)
        row(button("Open the playground", primary = true) { openPlayground(true) })
        gap(10)
        switchRow("Vibrate", null, prefs.haptics) { prefs.haptics = it }
        justPicked = null
    }

    private fun buildPlayground() {
        headline("Playground", null)
        val field = EditText(this).apply {
            // The sentence to read sits in the field itself: on the cover screen the keyboard
            // leaves room for little more than this field.
            hint = if (reading) "Tap here, hold the bubble and read the sentence below" else "Tap here, then hold the bubble and talk"
            setHintTextColor(DUST); setTextColor(PAPER); textSize = 17f; typeface = face(400)
            minLines = 3; gravity = Gravity.TOP
            background = rounded(SLATE, 0, 14)
            setPadding(px(14), px(12), px(14), px(12))
        }
        tryField = field
        field.setOnFocusChangeListener { v, has ->
            // Once the keyboard is up (the scroll padding grows by its height), bring the field to the top.
            if (has) scroll?.postDelayed({ scroll?.smoothScrollTo(0, v.top - px(8)) }, 350)
        }
        root.addView(field, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = px(10) })
        row(button("Clear") { field.setText("") }, button("Done") { openPlayground(false) })
        traceCard = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = rounded(CAPTION, 0, 10)
            setPadding(px(16), px(6), px(16), px(16))
        }.also { root.addView(it, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = px(14) }) }
        renderTrace()

        if (reading) {
            heading("Read this out")
            host.addView(TextView(this).apply {
                text = SENTENCES[sentence % SENTENCES.size]; textSize = 19f; setTextColor(PAPER); typeface = face(420)
                setLineSpacing(0f, 1.25f); setPadding(0, 0, 0, px(4))
            })
            small("Kept with the recording, so every take is scored. Keep the same sentence to compare microphones.")
            row(button("Next sentence") { sentence++; syncExpected(); build() },
                button("Say anything") { reading = false; syncExpected(); build() })
        } else {
            gap(8)
            row(button("Read sentences") { reading = true; syncExpected(); build() })
        }

        heading("Microphone")
        val mics = listOf(MediaRecorder.AudioSource.VOICE_RECOGNITION to ("Raw" to "The mic as it is."),
            MediaRecorder.AudioSource.VOICE_COMMUNICATION to ("Voice call" to "The phone's noise suppression and gain."),
            MediaRecorder.AudioSource.MIC to ("Standard" to "Android's default processing."))
        for ((src, lt) in mics) specimen(lt.first, lt.second, prefs.micSource == src, bigSample = false) { prefs.micSource = src; build() }
        heading("Style")
        for ((value, label) in styleOptions()) specimen(label, null, value == prefs.style) { prefs.style = value; build() }
        gap(6)
        switchRow("Polish with the LLM", null, prefs.llm) { prefs.llm = it; build() }
        learnSwitch()
        val stt = sttProfiles(health)
        if (stt.isNotEmpty()) {
            heading("Speech model")
            for (name in stt) specimen(name, null, name == prefs.profile, bigSample = false) { prefs.profile = name; build() }
        }
    }

    /** Opt-in edit learning (Prefs.learnEdits), with what it sends. */
    private fun learnSwitch() = switchRow("Learn from my edits",
        if (prefs.learnEdits) "When you're done with a dictation, the field's text around it (at most 4,000 characters, all of it in a short field) goes to your server, so it learns your fixes."
        else "Off: after a dictation goes in, the field isn't read again.", prefs.learnEdits) { prefs.learnEdits = it; build() }

    /** The last dictation (Trace): final text, what the speech model heard, each cleanup edit
     *  and whether the guard kept it, and where the time went. */
    private fun renderTrace() {
        val card = traceCard ?: return
        card.removeAllViews()
        fun line(t: String, color: Int = DUST, size: Float = 13.5f, top: Int = 2, bold: Boolean = false) =
            card.addView(TextView(this).apply {
                text = t; textSize = size; setTextColor(color); setPadding(0, px(top), 0, 0); setLineSpacing(0f, 1.15f)
                if (bold) typeface = Typeface.DEFAULT_BOLD
            })
        fun label(t: String) = line(t, DUST, 12.5f, 12)
        val e = Trace.last
        if (e == null) { line("Nothing yet. Tap the field, hold the bubble and talk.", DUST, 14f); return }
        val r = e.result
        if (r == null) { line("Not sent: ${e.error ?: "server unreachable"}", RED, 14f); return }
        val t = r.optJSONObject("timings_ms") ?: JSONObject()
        fun ms(k: String) = t.optDouble(k, Double.NaN).takeIf { !it.isNaN() }?.let { "${it.toInt()} ms" }
        val rep = e.report
        rep?.optString("expected")?.takeIf { it.isNotEmpty() }?.let { exp ->
            val off = wordsOff(exp, r.optString("raw_text"))
            label("You read")
            line(exp)
            line(if (off == 0) "Heard every word" else "$off of ${words(exp).size} words heard wrong", if (off == 0) YELLOW else RED)
        }
        label("What it typed, in ${r.optString("style")} style (${e.outcome})")
        card.addView(TextView(this).apply {
            val typed = r.optString("text").ifEmpty { "(nothing)" }
            text = typed; textSize = 19f; setTextColor(PAPER); typeface = face(420)
            setLineSpacing(0f, 1.2f); setPadding(0, px(4), 0, 0)
            if (revealedTrace !== e) { revealedTrace = e; reveal(this, typed, perWordMs = 55) }
        })
        e.edit?.let { ed ->
            label("Then · ${ed.reason.wire}")
            when {
                !ed.send -> line("Not sent (the dictation couldn't be found in the field)")
                !ed.changed -> line("Kept as is", YELLOW)
                else -> line("You made it: ${ed.finalText}", PAPER)
            }
        }
        label("${r.optString("profile")} heard")
        line(r.optString("raw_text").ifEmpty { "(nothing)" })
        val edits = r.optJSONArray("edits")
        label("Cleanup")
        if (edits == null || edits.length() == 0) line("No changes")
        else for (i in 0 until edits.length()) {
            val ed = edits.getJSONObject(i)
            val change = "${ed.optString("raw")} → ${ed.optString("cleaned").ifEmpty { "(removed)" }}"
            if (ed.optBoolean("accepted")) line("✓ ${ed.optString("reason")}: $change", YELLOW)
            else line("✗ reverted (${ed.optString("reason")}): $change", RED)
        }
        label("Timing")
        line(listOfNotNull(ms("wall")?.let { "speech $it" }, ms("llm")?.let { "LLM $it" }, "letting go → text ${e.roundTripMs} ms").joinToString(" · "))
        rep?.optJSONObject("marks")?.let { m ->
            line(listOfNotNull(m.optLong("first_frame", -1).takeIf { it >= 0 }?.let { "mic on in $it ms" },
                m.optLong("ready", -1).takeIf { it >= 0 }?.let { "server ready at $it ms" },
                rep.optString("path").takeIf { it == "upload" }?.let { "sent as an upload" }).joinToString(" · "))
        }
        label("Microphone")
        rep?.optJSONObject("mic")?.let { m ->
            val fx = (m.optJSONArray("effects")?.let { a -> (0 until a.length()).map { a.getString(it) } } ?: emptyList()) +
                (m.optJSONArray("client_effects")?.let { a -> (0 until a.length()).map { a.getString(it) } } ?: emptyList())
            line(listOfNotNull(MIC_LABELS[m.optString("source_name")] ?: m.optString("source_name"),
                m.optString("device").takeIf { it.isNotEmpty() && it != "null" },
                if (fx.isEmpty()) "no effects" else fx.joinToString(", ")).joinToString(" · "))
        }
        val audio = r.optInt("duration_ms") / 1000f
        val gain = t.optDouble("gain_db", 0.0)
        val a = r.optJSONObject("audio")
        if (a != null && a.has("speech_db")) {
            val speech = a.optDouble("speech_db"); val gap = a.optDouble("snr_db"); val rumble = a.optDouble("rumble_pct")
            line("%.1f s · speech %.0f dB · noise %.0f dB · gap %.0f dB · rumble %.0f%%".format(
                audio, speech, a.optDouble("noise_db"), gap, rumble))
            val hints = listOfNotNull("quiet".takeIf { speech < -35 }, "noisy (speech barely above the background)".takeIf { gap < 12 },
                "rumble from handling or wind".takeIf { rumble > 40 })
            if (hints.isNotEmpty()) line(hints.joinToString(", ").replaceFirstChar { it.uppercase() }, RED)
        } else {
            line("%.1f s of audio · boosted %.0f dB".format(audio, gain))
        }
    }

    private fun words(t: String) = t.lowercase().split(Regex("\\s+")).map { it.trim { c -> !c.isLetterOrDigit() && c != '\'' } }.filter { it.isNotEmpty() }

    /** Word edit distance between what was read out and what the speech model heard. */
    private fun wordsOff(expected: String, heard: String): Int {
        val a = words(expected); val b = words(heard)
        var prev = IntArray(b.size + 1) { it }
        for (i in 1..a.size) {
            val cur = IntArray(b.size + 1); cur[0] = i
            for (j in 1..b.size) cur[j] = minOf(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + if (a[i - 1] == b[j - 1]) 0 else 1)
            prev = cur
        }
        return prev[b.size]
    }

    /** The Flip cover screen's camera band. Samsung's Flip 7 cover-display emulator skin puts the
     *  flash and lenses at x 457-907, y 843-1017 of 948x1048 (48% across, bottom 19.6%); 22% leaves
     *  a margin. */
    private fun cameraBand(): Int {
        val b = getSystemService(WindowManager::class.java).currentWindowMetrics.bounds
        val wDp = b.width() / d; val hDp = b.height() / d; val aspect = b.height().toFloat() / b.width()
        if (wDp > 630 || hDp > 700 || aspect !in 0.75f..1.334f) return 0
        return (b.height() * if (aspect >= 1.07f) 0.22f else 0.10f).toInt()
    }

    private val faceCache = HashMap<Int, Typeface>()

    /** Atkinson Hyperlegible Next (assets/fonts, variable weight): made to be read at a glance,
     *  like captions. */
    private fun face(wght: Int): Typeface = faceCache.getOrPut(wght) {
        Typeface.Builder(assets, "fonts/atkinson.ttf").setFontVariationSettings("'wght' $wght").build() ?: Typeface.DEFAULT
    }

    /** The top of a screen, as a caption: a waveform and what's going on, in caption yellow on
     *  caption black (red when something's wrong). */
    private fun headline(t: String, sub: String?, alarm: Boolean = false) {
        root.addView(LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            background = rounded(CAPTION, 0, 10); setPadding(px(16), px(14), px(18), px(14))
            val wave = WaveView(context, if (alarm) RED else YELLOW)
            addView(wave, LinearLayout.LayoutParams(px(40), px(30)).apply { marginEnd = px(14) })
            val caption = TextView(context).apply {
                text = t; setTextColor(if (alarm) RED else YELLOW); textSize = if (compact) 21f else 25f; typeface = face(760)
                setLineSpacing(0f, 1.05f)
            }
            addView(caption, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            if (t != shownHeadline && animationsOn) {
                // Said, then captioned: the bars talk while the words come in.
                val ms = reveal(caption, t, perWordMs = 110, startMs = 180)
                wave.talk(ms + 380)
            }
            shownHeadline = t
        }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply {
            topMargin = px(if (compact) 4 else 16)
        })
        sub?.takeIf { it.isNotBlank() }?.let { s -> root.addView(TextView(this).apply {
            text = s; textSize = 15f; setTextColor(DUST); typeface = face(400); setLineSpacing(0f, 1.25f); setPadding(px(2), px(10), px(24), 0)
        }) }
    }

    /** A still waveform, drawn: the thing that becomes text. */
    private class WaveView(ctx: android.content.Context, private val tint: Int) : View(ctx) {
        private val rest = floatArrayOf(0.30f, 0.55f, 0.85f, 0.45f, 1f, 0.65f, 0.35f, 0.75f, 0.5f, 0.25f)
        private val bars = rest.copyOf()

        /** Move like speech for `ms`, then settle back to the resting shape. */
        fun talk(ms: Long) {
            android.animation.ValueAnimator.ofFloat(0f, 1f).apply {
                duration = ms; interpolator = android.view.animation.LinearInterpolator()
                addUpdateListener { a ->
                    val f = a.animatedFraction; val t = f * ms / 1000f
                    val settle = if (f > 0.8f) (1f - f) / 0.2f else 1f // ease back to rest at the end
                    for (i in bars.indices) {
                        val speech = 0.2f + 0.8f * kotlin.math.abs(kotlin.math.sin(t * (7f + i % 3 * 2.3f) + i * 1.7f))
                        bars[i] = rest[i] + (speech - rest[i]) * settle
                    }
                    invalidate()
                }
                doOnEnd { rest.copyInto(bars); invalidate() }
                start()
            }
        }

        private fun android.animation.Animator.doOnEnd(f: () -> Unit) = addListener(object : android.animation.AnimatorListenerAdapter() {
            override fun onAnimationEnd(a: android.animation.Animator) = f()
        })
        private val paint = android.graphics.Paint(android.graphics.Paint.ANTI_ALIAS_FLAG).apply {
            color = tint; strokeCap = android.graphics.Paint.Cap.ROUND
        }
        override fun onDraw(c: android.graphics.Canvas) {
            val step = width.toFloat() / bars.size
            paint.strokeWidth = step * 0.55f
            val mid = height / 2f
            bars.forEachIndexed { i, h ->
                val x = step * (i + 0.5f); val half = (height * 0.5f - paint.strokeWidth / 2) * h
                c.drawLine(x, mid - half, x, mid + half, paint)
            }
        }
    }

    private fun heading(t: String) = root.addView(TextView(this).apply {
        text = t; textSize = 19f; setTextColor(PAPER); typeface = face(720); setPadding(px(2), px(30), 0, px(8))
    })

    private fun small(t: String, indent: Int = 0) = host.addView(TextView(this).apply {
        text = t; textSize = 13.5f; setTextColor(DUST); setLineSpacing(0f, 1.25f); setPadding(px(indent), px(2), 0, px(2))
    })

    private fun body(t: String) = small(t)

    private fun gap(dp: Int) = root.addView(View(this), LinearLayout.LayoutParams(1, px(dp)))

    private fun note(t: String) = android.widget.Toast.makeText(this, t, android.widget.Toast.LENGTH_LONG).show()

    private fun statusLine(t: String, ok: Boolean) = host.addView(TextView(this).apply {
        text = t; textSize = 14f; setTextColor(if (ok) YELLOW else RED); setPadding(0, 0, 0, px(8))
    })

    private fun field(value: String, hintText: String, variation: Int) = EditText(this).apply {
        setText(value); hint = hintText; setHintTextColor(DUST); setTextColor(PAPER); textSize = 15f; isSingleLine = true
        inputType = InputType.TYPE_CLASS_TEXT or variation
        typeface = Typeface.DEFAULT // a password field would switch to monospace
        background = rounded(SLATE, 0, 12); setPadding(px(14), px(12), px(14), px(12))
    }.also {
        host.addView(it, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { bottomMargin = px(8) })
    }

    private fun button(label: String, primary: Boolean = false, onClick: () -> Unit) = Button(this).apply {
        text = label; isAllCaps = false; textSize = 15f; stateListAnimator = null
        setTextColor(if (primary) CAPTION else PAPER); typeface = face(650)
        background = rounded(if (primary) YELLOW else 0, if (primary) 0 else EDGE, 24)
        minHeight = px(46); minimumHeight = px(46); setPadding(px(18), 0, px(18), 0)
        setOnClickListener { onClick() }
    }

    private fun textButton(label: String, indent: Int = 0, onClick: () -> Unit) = host.addView(TextView(this).apply {
        text = label; textSize = 14.5f; setTextColor(YELLOW); setPadding(px(indent), px(6), px(16), px(12))
        setOnClickListener { onClick() }
    }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT))

    private fun row(vararg views: View) = host.addView(LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL; setPadding(0, px(6), 0, 0)
        views.forEachIndexed { i, v ->
            addView(v, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f).apply { if (i > 0) marginStart = px(10) })
        }
    })

    /** A value on its own line with one action after it. */
    private fun line(value: String, action: String, onClick: () -> Unit) = host.addView(LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
        addView(TextView(context).apply { text = value; textSize = 16f; setTextColor(PAPER) },
            LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        addView(TextView(context).apply {
            text = action; textSize = 14.5f; setTextColor(YELLOW); setPadding(px(16), px(10), 0, px(10)); setOnClickListener { onClick() }
        })
    })

    /** Something still to do: what, why, and the one button that does it. Red pencil in the margin. */
    private fun todo(label: String, why: String, action: String, onClick: () -> Unit) = host.addView(LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
        setPadding(px(2), px(10), 0, px(10))
        addView(View(context).apply { background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(RED) } },
            LinearLayout.LayoutParams(px(10), px(10)).apply { marginEnd = px(14) })
        addView(LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            addView(TextView(context).apply { text = label; textSize = 16f; setTextColor(PAPER); typeface = face(600) })
            addView(TextView(context).apply { text = why; textSize = 13.5f; setTextColor(DUST) })
        }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f).apply { marginEnd = px(12) })
        addView(Button(context).apply {
            text = action; isAllCaps = false; textSize = 14f; stateListAnimator = null; setTextColor(RED)
            background = GradientDrawable().apply { cornerRadius = 20 * d; setStroke(px(1) + 1, RED) }
            minHeight = px(38); minimumHeight = px(38); setPadding(px(16), 0, px(16), 0)
            setOnClickListener { onClick() }
        })
    }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { bottomMargin = px(6) })

    /** One choice: its name, and under it what it gives you. The chosen one has a caption-yellow
     *  text cursor after its name (where your words go) and full contrast; the others stay quiet. */
    private fun specimen(name: String, sample: String?, on: Boolean, bigSample: Boolean = true, reveal: Boolean = false,
                         onPick: (String) -> Unit): View {
        val v = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(px(2), px(10), px(4), px(10))
            addView(LinearLayout(context).apply {
                orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
                addView(TextView(context).apply {
                    text = name; textSize = 15f; setTextColor(if (on) PAPER else DUST); typeface = face(if (on) 700 else 500)
                })
                if (on) addView(View(context).apply {
                    setBackgroundColor(YELLOW)
                    if (animationsOn) startAnimation(android.view.animation.AlphaAnimation(1f, 0f).apply {
                        duration = 530; repeatMode = android.view.animation.Animation.REVERSE
                        repeatCount = android.view.animation.Animation.INFINITE
                        interpolator = android.view.animation.Interpolator { if (it < 0.5f) 0f else 1f }
                    })
                }, LinearLayout.LayoutParams(px(3), px(19)).apply { marginStart = px(5) })
            })
            sample?.let { t -> addView(TextView(context).apply {
                // Folded, a sample shows its words on two lines; chosen, it shows its layout too.
                text = if (on) t else t.replace(Regex("\\s*\\n+\\s*"), " ")
                setLineSpacing(0f, 1.25f); setPadding(0, px(4), 0, 0); typeface = face(400)
                textSize = if (bigSample) 16.5f else 14.5f
                setTextColor(if (on) PAPER else FADED)
                if (!on) { maxLines = 2; ellipsize = android.text.TextUtils.TruncateAt.END }
                if (on && reveal) reveal(this, t, perWordMs = 45)
            }) }
            isClickable = true; setOnClickListener { onPick(name) }
        }
        host.addView(v, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
        return v
    }

    /** Writes `full` into `tv` a word at a time, like live captions; the rest is laid out but
     *  invisible, so nothing jumps. Returns how long it takes. Instant with animations off. */
    private fun reveal(tv: TextView, full: String, perWordMs: Long, startMs: Long = 0): Long {
        if (!animationsOn) { tv.text = full; return 0 }
        val ends = Regex("\\S+").findAll(full).map { it.range.last + 1 }.toList()
        fun upTo(end: Int) = android.text.SpannableString(full).apply {
            if (end < full.length) setSpan(android.text.style.ForegroundColorSpan(android.graphics.Color.TRANSPARENT), end, full.length, 0)
        }
        val token = Any(); tv.tag = token
        tv.text = upTo(0)
        ends.forEachIndexed { i, end -> tv.postDelayed({ if (tv.tag === token) tv.text = upTo(end) }, startMs + i * perWordMs) }
        return startMs + ends.size * perWordMs
    }

    /** Off when the system's animations are off (reduced motion). */
    private val animationsOn get() = Settings.Global.getFloat(contentResolver, Settings.Global.ANIMATOR_DURATION_SCALE, 1f) > 0f

    private fun switchRow(label: String, sub: String?, on: Boolean, onChange: (Boolean) -> Unit) {
        host.addView(LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL; minimumHeight = px(52)
            addView(LinearLayout(context).apply {
                orientation = LinearLayout.VERTICAL
                addView(TextView(context).apply { text = label; textSize = 16f; setTextColor(PAPER) })
                sub?.let { addView(TextView(context).apply { text = it; textSize = 13.5f; setTextColor(DUST); setLineSpacing(0f, 1.2f) }) }
            }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f).apply { marginEnd = px(16) })
            addView(Switch(context).apply {
                isChecked = on; setOnCheckedChangeListener { _, v -> onChange(v) }
                thumbTintList = android.content.res.ColorStateList(arrayOf(intArrayOf(android.R.attr.state_checked), intArrayOf()), intArrayOf(YELLOW, DUST))
                trackTintList = android.content.res.ColorStateList(arrayOf(intArrayOf(android.R.attr.state_checked), intArrayOf()), intArrayOf(0x66FFD24A, SLATE))
            })
        })
    }

    /** The Flip's cover screen and other short windows get a smaller headline. */
    private val compact get() = resources.configuration.screenHeightDp < 600

    private fun rounded(fill: Int, stroke: Int, radiusDp: Int) = GradientDrawable().apply {
        setColor(fill); cornerRadius = radiusDp * d
        if (stroke != 0) setStroke(px(1), stroke)
    }

    companion object {
        private val MIC_LABELS = mapOf("raw" to "Raw", "call" to "Voice call", "standard" to "Standard")
        /** Everyday dictation: questions, a list, negations. No numbers: the speech model writes them
         *  as digits or words unpredictably, which would count as misheard. */
        private val SENTENCES = listOf(
            "Hey, can you send me the budget report by Friday? I'll review it over the weekend.",
            "I'm running a little late, so start the meeting without me.",
            "Remind me to call the dentist tomorrow afternoon.",
            "Please don't merge the pull request until the tests pass.",
            "We need eggs, oat milk, bread and a packet of coffee.",
            "My flight lands early, so I'll be home before dinner.",
            "Thanks for the quick reply, that works for me.",
            "Can you check whether the server is still running on the desktop?",
            "I think the second option is cheaper, but the first one is faster.",
            "Let's grab dinner on Saturday, maybe somewhere near the station.",
            "The new build fixes the crash when you open it on the cover screen.",
            "I never said we should cancel the trip, just move it to next month.",
        )
        /** One thing said, as each style would type it (illustrative; the server does the real work). */
        private const val SPOKEN = "so um for the trip I need to like book the train, oh and the hotel near the station, " +
            "which honestly is kind of ugly but whatever, and renew my passport"
        private val SAMPLES = mapOf(
            "app" to "Message in chat apps, Email in mail, Note in notes, Plain everywhere else.",
            "auto" to "For the trip I need to book the train, and the hotel near the station, which honestly is kind of ugly, " +
                "but whatever, and renew my passport.",
            "message" to "For the trip I need to book the train, and the hotel near the station, which is kind of ugly, " +
                "and renew my passport.",
            "note" to "For the trip I need to:\n•  book the train\n•  get the hotel near the station\n•  renew my passport",
            "email" to "Hi,\n\nFor the trip I need to book the train and the hotel near the station, and renew my passport.\n\nThanks",
            "ramble" to "Before the trip: book the train, get the hotel near the station, and renew my passport.",
        )
        // Captions: yellow on black for what's chosen and the state, red for live and for what
        // needs you, on warm charcoal.
        private const val INK = 0xFF1B1815.toInt()
        private const val CAPTION = 0xFF0D0C0B.toInt()
        private const val SLATE = 0xFF27231F.toInt()
        private const val EDGE = 0xFF3D3731.toInt()
        private const val PAPER = 0xFFF2EEE8.toInt()
        private const val DUST = 0xFF9B9389.toInt()
        private const val FADED = 0xFF6F685F.toInt()
        private const val YELLOW = 0xFFFFD24A.toInt()
        private const val RED = 0xFFFF5A4C.toInt()
        private const val BG = INK
    }
}
