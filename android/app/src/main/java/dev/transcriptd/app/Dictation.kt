package dev.transcriptd.app

import android.os.Handler
import android.os.Looper
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.io.ByteArrayOutputStream

/** One dictation against /v1/dictate (server/transcriptd/api/dictate.py).
 *
 *  The socket opens while the mic starts; frames are buffered until the server says `ready`,
 *  so the first words are never lost. Every frame is also kept here: if the socket fails, the
 *  same take is uploaded to /v1/transcribe instead, and a failed upload can be retried. */
class Dictation(
    private val prefs: Prefs,
    private val app: String?,
    private val context: JSONObject?,
    val style: String,
    private val listener: Listener,
    private val report: Report,
) {
    interface Listener {
        fun onResult(result: JSONObject)
        fun onFailure(message: String)
    }

    private val main = Handler(Looper.getMainLooper())
    private val pcm = ByteArrayOutputStream()
    private val pending = ArrayList<ByteArray>()
    private var ws: WebSocket? = null
    private var ready = false
    private var ended = false
    private var settled = false
    private var uploading = false
    private var socketDead = false

    val seconds: Float get() = synchronized(this) { pcm.size() / 32000f }

    fun open() {
        // A server address the app won't use (ServerUrl): the upload refuses it too, and says why.
        val req = try { Api.dictateRequest(prefs) } catch (e: Exception) { report.put("socket_error", e.message); socketFailed(); return }
        ws = Api.http.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                report.mark("socket_open")
                val start = JSONObject()
                    .put("type", "start")
                    .put("style", style)
                    .put("llm", prefs.llm)
                    .put("partials", false)
                    .put("app_hint", app ?: "phone")
                if (prefs.profile.isNotEmpty()) start.put("profile", prefs.profile)
                if (context != null) start.put("context", context)
                webSocket.send(start.toString())
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val m = JSONObject(text)
                when (m.optString("type")) {
                    "ready" -> synchronized(this@Dictation) {
                        report.mark("ready")
                        for (f in pending) webSocket.send(f.toByteString())
                        pending.clear()
                        ready = true
                        if (ended) { webSocket.send("""{"type":"end"}"""); report.mark("end_sent") }
                    }
                    "final" -> { settle { listener.onResult(m) }; webSocket.close(1000, null) }
                    "error" -> { report.put("socket_error", m.optString("message")); socketFailed(); webSocket.close(1000, null) }
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                if (!settled) report.put("socket_error", t.toString())
                socketFailed()
            }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = socketFailed()
        })
    }

    fun frame(bytes: ByteArray) = synchronized(this) {
        if (ended) return@synchronized
        pcm.write(bytes)
        if (ready) ws?.send(bytes.toByteString()) else pending.add(bytes)
    }

    /** The user is done talking. */
    fun end() {
        synchronized(this) {
            ended = true
            if (ready) { ws?.send("""{"type":"end"}"""); report.mark("end_sent") }
        }
        if (pcm.size() < 16000 * 2 * 3 / 10) { // under 0.3 s
            ws?.cancel()
            settle { listener.onResult(JSONObject().put("empty", true)) }
            return
        }
        // The socket already failed: upload now. Still connecting after a few seconds: give up on
        // it and upload the take.
        if (socketDead) { upload(); return }
        main.postDelayed({ if (!ready && !settled) { ws?.cancel(); upload() } }, 6_000)
        main.postDelayed({ if (!settled) { ws?.cancel(); upload() } }, 90_000)
    }

    fun cancel() {
        settled = true
        ws?.send("""{"type":"cancel"}""")
        ws?.close(1000, null)
        main.removeCallbacksAndMessages(null)
    }

    /** After a failure: send the kept audio again. */
    fun retry() {
        settled = false
        upload()
    }

    private fun socketFailed() {
        socketDead = true
        if (settled) return // a normal close after the final
        report.mark("socket_failed")
        if (ended) upload() // otherwise end() uploads once the user stops talking
    }

    private fun upload() {
        synchronized(this) {
            if (uploading || settled) return
            uploading = true
        }
        report.mark("upload")
        report.put("path", "upload")
        report.put("uploads", report.json().optInt("uploads") + 1)
        val audio = synchronized(this) { pcm.toByteArray() }
        Thread {
            try {
                val r = Api.transcribe(prefs, audio, style, app, context)
                settle { listener.onResult(r) }
            } catch (e: Exception) {
                settle(final = false) { listener.onFailure(e.message ?: "Server unreachable") }
            } finally {
                synchronized(this) { uploading = false }
            }
        }.start()
    }

    private fun settle(final: Boolean = true, block: () -> Unit) {
        synchronized(this) {
            if (settled) return
            if (final) settled = true
        }
        main.removeCallbacksAndMessages(null)
        main.post(block)
    }
}
