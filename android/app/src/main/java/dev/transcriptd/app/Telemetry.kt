package dev.transcriptd.app

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.Build
import android.os.SystemClock
import android.util.Log
import android.view.Display
import android.view.WindowManager
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/** One take's report for the server's /v1/telemetry (server/transcriptd/api/routes.py): the
 *  phone, the screen, the microphone Android actually gave us, when each step happened, and how
 *  the text went in. The server keeps it next to its own side of the take (what the model heard,
 *  the audio and its levels), so a bad dictation can be traced to the mic, the network or the
 *  model. Thread-safe; `mark` times are ms since the bubble was pressed. */
class Report(context: Context, prefs: Prefs) {
    private val t0 = SystemClock.uptimeMillis()
    private val data = JSONObject()
    private val marks = JSONObject()
    private var sent = false

    init {
        data.put("client", "android")
            .put("at", System.currentTimeMillis()) // when it was said; queued reports arrive later
            .put("app_version", BuildConfig.VERSION_NAME)
            .put("device", JSONObject().put("maker", Build.MANUFACTURER).put("model", Build.MODEL).put("sdk", Build.VERSION.SDK_INT))
            .put("style_pref", prefs.style).put("llm", prefs.llm).put("profile", prefs.profile)
            .put("network", network(context))
            .put("display", display(context))
            .put("marks", marks)
    }

    fun mark(name: String) = synchronized(this) { if (!marks.has(name)) marks.put(name, SystemClock.uptimeMillis() - t0) }
    fun put(key: String, value: Any?) = synchronized(this) { data.put(key, value ?: JSONObject.NULL) }
    fun has(key: String) = synchronized(this) { data.has(key) }
    fun json(): JSONObject = synchronized(this) { JSONObject(data.toString()) }

    /** Sends it once; later calls do nothing. */
    fun send(context: Context, prefs: Prefs) {
        val body = synchronized(this) { if (sent) return; sent = true; data.toString() }
        Telemetry.send(context.applicationContext, prefs, body)
    }

    private fun network(context: Context): JSONArray {
        val cm = context.getSystemService(ConnectivityManager::class.java)
        val caps = try { cm.getNetworkCapabilities(cm.activeNetwork) } catch (_: Exception) { null } ?: return JSONArray()
        return JSONArray(listOf(
            NetworkCapabilities.TRANSPORT_WIFI to "wifi", NetworkCapabilities.TRANSPORT_CELLULAR to "cellular",
            NetworkCapabilities.TRANSPORT_VPN to "vpn", NetworkCapabilities.TRANSPORT_ETHERNET to "ethernet",
        ).filter { caps.hasTransport(it.first) }.map { it.second })
    }

    /** Which display and what the system says about it: the Flip's cover is display 1, and its
     *  cameras should come back as a cutout. Logged so the bubble's hard-coded band can be checked
     *  against what One UI actually reports before relying on it. */
    private fun display(context: Context): JSONObject {
        val o = JSONObject()
        try {
            val wm = context.getSystemService(WindowManager::class.java)
            val win = wm.currentWindowMetrics.bounds
            val max = wm.maximumWindowMetrics.bounds
            val dm = context.resources.displayMetrics
            o.put("window_px", "${win.width()}x${win.height()}").put("panel_px", "${max.width()}x${max.height()}")
                .put("density_dpi", dm.densityDpi)
            // Every display that's on (folded: the cover, id 1), with what it reports as cutout.
            val dmgr = context.getSystemService(android.hardware.display.DisplayManager::class.java)
            o.put("on", JSONArray(dmgr.displays.filter { it.state == Display.STATE_ON }.map { disp ->
                val mode = disp.mode
                JSONObject().put("id", disp.displayId).put("px", "${mode.physicalWidth}x${mode.physicalHeight}")
                    .put("cutout", disp.cutout?.let { c ->
                        JSONObject().put("rects", JSONArray(c.boundingRects.map { "${it.left},${it.top}-${it.right},${it.bottom}" }))
                            .put("safe", "${c.safeInsetLeft},${c.safeInsetTop},${c.safeInsetRight},${c.safeInsetBottom}")
                    } ?: JSONObject.NULL)
            }))
            val insets = wm.currentWindowMetrics.windowInsets.getInsets(android.view.WindowInsets.Type.displayCutout())
            o.put("cutout_insets", "${insets.left},${insets.top},${insets.right},${insets.bottom}")
        } catch (e: Exception) {
            o.put("error", e.toString())
        }
        return o
    }
}

/** Delivery: posts reports, and keeps the ones that couldn't be sent (the server unreachable is
 *  itself worth reporting) in files/telemetry-queue.jsonl until the next one goes through. */
object Telemetry {
    private const val KEEP = 300
    private val lock = Any()

    fun send(context: Context, prefs: Prefs, body: String) {
        Thread {
            val queue = File(context.filesDir, "telemetry-queue.jsonl")
            if (!post(prefs, body)) {
                synchronized(lock) {
                    val lines = (if (queue.exists()) queue.readLines() else emptyList()) + body
                    queue.writeText(lines.takeLast(KEEP).joinToString("\n", postfix = "\n"))
                }
                return@Thread
            }
            synchronized(lock) {
                if (!queue.exists()) return@Thread
                val left = queue.readLines().filter { it.isNotBlank() && !post(prefs, it) }
                if (left.isEmpty()) queue.delete() else queue.writeText(left.joinToString("\n", postfix = "\n"))
            }
        }.start()
    }

    private fun post(prefs: Prefs, body: String): Boolean = try {
        Api.report(prefs, body); true
    } catch (e: Exception) {
        Log.w("transcriptd", "telemetry not sent: ${e.message}"); false
    }
}
