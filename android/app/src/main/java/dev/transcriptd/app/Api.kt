package dev.transcriptd.app

import okhttp3.HttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.util.concurrent.TimeUnit

/** The transcriptd server's HTTP and WebSocket API (server/transcriptd/api). Blocking calls;
 *  run them off the main thread. */
object Api {
    /** The one client for the server, WebSocket included. Redirects aren't followed: a hop
     *  isn't checked against ServerUrl, and a 307/308 would resend the audio or edit (and the
     *  token) to wherever it points, plain http:// included. A redirect is an error instead. */
    val http: OkHttpClient = OkHttpClient.Builder()
        .followRedirects(false)
        .followSslRedirects(false)
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .pingInterval(15, TimeUnit.SECONDS)
        .build()

    /** The token only when there is one; your own Tailscale devices don't need it. */
    private fun Request.Builder.auth(p: Prefs) = apply { if (p.token.isNotEmpty()) header("Authorization", "Bearer ${p.token}") }

    /** The server's base URL. Refuses plain http:// to anything off your own network
     *  (ServerUrl), so nothing (audio, text, the token) crosses the internet unencrypted. */
    private fun base(p: Prefs): HttpUrl {
        ServerUrl.problem(p.server)?.let { throw IOException(it) }
        return ServerUrl.parse(p.server)!!
    }

    fun dictateRequest(p: Prefs): Request {
        // OkHttp takes http(s) URLs for WebSockets and upgrades them itself.
        val url = base(p).newBuilder().addPathSegments("v1/dictate").build()
        return Request.Builder().url(url).auth(p).build()
    }

    /** Throws unless [r] succeeded; a redirect names where it pointed, to put in the settings. */
    private fun check(r: Response) {
        if (r.isSuccessful) return
        if (r.isRedirect) throw IOException("Server redirected to ${r.header("Location") ?: "another address"}; set that as the server instead")
        throw IOException("Server answered ${r.code}")
    }

    fun health(p: Prefs): JSONObject = get(p, "v1/health")

    private fun get(p: Prefs, path: String): JSONObject {
        val req = Request.Builder().url(base(p).newBuilder().addPathSegments(path).build())
            .auth(p).build()
        http.newCall(req).execute().use { r ->
            if (r.code == 401) throw IOException(if (p.token.isEmpty()) "this phone isn't trusted (paste a pairing link)" else "the token was refused")
            check(r)
            return JSONObject(r.body!!.string())
        }
    }

    /** Fallback when the socket fails: send the whole take as a WAV. */
    fun transcribe(p: Prefs, pcm: ByteArray, style: String, app: String?, context: JSONObject?): JSONObject {
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("file", "phone.wav", wav(pcm).toRequestBody("audio/wav".toMediaType()))
            .addFormDataPart("save", "true")
            .addFormDataPart("llm", p.llm.toString())
            .addFormDataPart("style", style)
            .apply { if (p.profile.isNotEmpty()) addFormDataPart("profile", p.profile) }
            .apply { if (app != null) addFormDataPart("app_hint", app) }
            .apply { if (context != null) addFormDataPart("context", context.toString()) }
            .build()
        val req = Request.Builder().url(base(p).newBuilder().addPathSegments("v1/transcribe").build())
            .auth(p).post(body).build()
        http.newCall(req).execute().use { r ->
            check(r)
            return JSONObject(r.body!!.string())
        }
    }

    /** A take's telemetry (Telemetry.kt) to /v1/telemetry. */
    fun report(p: Prefs, json: String) {
        val req = Request.Builder().url(base(p).newBuilder().addPathSegments("v1/telemetry").build())
            .auth(p).post(json.toRequestBody("application/json".toMediaType())).build()
        http.newCall(req).execute().use { check(it) }
    }

    /** What became of a dictation once you were done with it (EditWatch.kt), so the server can
     *  learn from what you changed: {inserted_text, final_text, app, reason}, where final_text is
     *  a window of the field around the dictation (at most 4,000 characters); when you kept it as
     *  it went in, {final_text: null, app, reason} with no text. Only with "Learn from my edits" on. */
    fun edited(p: Prefs, id: Int, body: JSONObject) {
        val req = Request.Builder().url(base(p).newBuilder().addPathSegments("v1/dictations/$id/edited").build())
            .auth(p).post(body.toString().toRequestBody("application/json".toMediaType())).build()
        http.newCall(req).execute().use { check(it) }
    }

    fun wav(pcm: ByteArray): ByteArray {
        val out = ByteArrayOutputStream(44 + pcm.size)
        fun s(v: String) = out.write(v.toByteArray(Charsets.US_ASCII))
        fun i32(v: Int) = out.write(byteArrayOf(v.toByte(), (v shr 8).toByte(), (v shr 16).toByte(), (v shr 24).toByte()))
        fun i16(v: Int) = out.write(byteArrayOf(v.toByte(), (v shr 8).toByte()))
        s("RIFF"); i32(36 + pcm.size); s("WAVE"); s("fmt "); i32(16); i16(1); i16(1)
        i32(16000); i32(32000); i16(2); i16(16); s("data"); i32(pcm.size)
        out.write(pcm)
        return out.toByteArray()
    }
}
