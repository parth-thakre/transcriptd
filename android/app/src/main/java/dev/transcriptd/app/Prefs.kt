package dev.transcriptd.app

import android.content.Context
import android.media.MediaRecorder

/** Settings, kept in the app's private storage. The token never leaves the phone except to
 *  the server it belongs to. */
class Prefs(context: Context) {
    private val sp = context.getSharedPreferences("transcriptd", Context.MODE_PRIVATE)

    var server: String
        get() = sp.getString("server", null) ?: BuildConfig.DEFAULT_SERVER
        set(v) = sp.edit().putString("server", v.trim().trimEnd('/')).apply()
    var token: String
        get() = sp.getString("token", "") ?: ""
        set(v) = sp.edit().putString("token", v.trim()).apply()
    /** "app" picks a style from the app you are typing in; otherwise a server style name. */
    var style: String
        get() = sp.getString("style", "app") ?: "app"
        set(v) = sp.edit().putString("style", v).apply()
    /** Speech profile; empty until the setup screen picks a Whisper profile from the server. */
    var profile: String
        get() = sp.getString("profile", "") ?: ""
        set(v) = sp.edit().putString("profile", v).apply()
    /** The server's guarded LLM pass (punctuation, capitals, digits) after the speech model. */
    var llm: Boolean
        get() = sp.getBoolean("llm", true)
        set(v) = sp.edit().putBoolean("llm", v).apply()
    /** AudioRecord source: VOICE_RECOGNITION (6, raw on most phones), VOICE_COMMUNICATION (7, the
     *  phone's call processing: noise suppression and gain), MIC (1). Compared in the playground. */
    var micSource: Int
        get() = sp.getInt("micSource", MediaRecorder.AudioSource.VOICE_RECOGNITION)
        set(v) = sp.edit().putInt("micSource", v).apply()
    /** Learn from my edits: after a dictation goes in, watch the field until you're done with it
     *  and send the server a window around the dictation (EditWatch), so it learns your fixes.
     *  Off by default: with it off, nothing in the field is read after the insertion. */
    var learnEdits: Boolean
        get() = sp.getBoolean("learnEdits", false)
        set(v) = sp.edit().putBoolean("learnEdits", v).apply()
    var haptics: Boolean
        get() = sp.getBoolean("haptics", true)
        set(v) = sp.edit().putBoolean("haptics", v).apply()

    /** A server is set. The token is optional: with tailnet_auth the server knows your devices. */
    val paired get() = server.isNotBlank()

    /** Where the bubble sits, per screen (cover or main): the side, and how far above the
     *  keyboard in dp. */
    fun bubbleRight(screen: String) = sp.getBoolean("right.$screen", true)
    // Default: right edge, clear of the row above the keyboard where apps put their send button.
    fun bubbleLift(screen: String) = sp.getFloat("lift.$screen", 64f)
    fun saveBubble(screen: String, right: Boolean, lift: Float) =
        sp.edit().putBoolean("right.$screen", right).putFloat("lift.$screen", lift).apply()
}
