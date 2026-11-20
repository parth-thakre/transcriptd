package dev.transcriptd.app

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioRecordingConfiguration
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.AutomaticGainControl
import android.media.audiofx.NoiseSuppressor
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import kotlin.math.abs
import kotlin.math.max

/** 16 kHz mono 16-bit PCM in 20 ms frames, the format /v1/dictate takes.
 *  Debug builds read `files/test.wav` (16 kHz mono) instead of the mic when it exists, in real
 *  time, so the whole path can be tested on an emulator. */
class Recorder(
    private val context: Context,
    private val source: Int,
    private val report: Report?,
    private val onFrame: (ByteArray, Float) -> Unit,
    private val onError: (String) -> Unit,
) {
    @Volatile private var running = false
    private var thread: Thread? = null
    private var peak = 0f
    private var frames = 0

    fun start() {
        running = true
        val test = if (BuildConfig.DEBUG) File(context.getExternalFilesDir(null), "test.wav").takeIf { it.exists() } else null
        report?.put("mic", JSONObject().put("source", source).put("source_name", sourceName(source)).put("test_file", test != null))
        thread = Thread({ if (test != null) playFile(test) else recordMic() }, "transcriptd-mic").apply { start() }
    }

    /** Stops and waits for the last frame, so nothing said is cut off. */
    fun stop() {
        running = false
        thread?.join(600)
        thread = null
        report?.put("client_audio", JSONObject().put("frames", frames).put("seconds", frames / 50.0)
            .put("peak_db", if (peak > 0) Math.round(200 * Math.log10(peak.toDouble())) / 10.0 else -120.0))
    }

    @SuppressLint("MissingPermission") // checked by FlowService before starting
    private fun recordMic() {
        val min = AudioRecord.getMinBufferSize(16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        // Prefs.micSource; VOICE_RECOGNITION by default (no gain or noise shaping on most phones).
        val rec = try {
            AudioRecord(source, 16000, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT, max(min, FRAME * 10))
        } catch (e: Exception) {
            onError("Could not open the microphone"); return
        }
        if (rec.state != AudioRecord.STATE_INITIALIZED) { rec.release(); onError("Could not open the microphone"); return }
        report?.mark("mic_open")
        try {
            rec.startRecording()
            val buf = ByteArray(FRAME)
            var checked = false
            while (running) {
                if (!checked && rec.activeRecordingConfiguration != null) {
                    checked = true
                    describe(rec.activeRecordingConfiguration!!)
                    // Android hands a blocked recorder silence rather than an error; say so.
                    if (rec.activeRecordingConfiguration!!.isClientSilenced) {
                        Log.w("transcriptd", "microphone silenced by the system")
                        onError("Android blocked the microphone for the bubble")
                        break
                    }
                }
                var n = 0
                while (n < FRAME) {
                    val r = rec.read(buf, n, FRAME - n)
                    if (r <= 0) break
                    n += r
                }
                if (n > 0) emit(buf.copyOf(n))
            }
        } finally {
            try { rec.stop() } catch (_: Exception) {}
            rec.release()
        }
    }

    private fun playFile(file: File) {
        val data = file.readBytes()
        var at = 44
        while (running && at < data.size) {
            val end = minOf(at + FRAME, data.size)
            emit(data.copyOfRange(at, end))
            at = end
            Thread.sleep(20)
        }
        while (running) Thread.sleep(20)
    }

    private fun emit(frame: ByteArray) {
        var p = 0
        for (i in 0 until frame.size - 1 step 2) {
            val s = (frame[i].toInt() and 0xff) or (frame[i + 1].toInt() shl 8)
            p = max(p, abs(s.toShort().toInt()))
        }
        if (frames++ == 0) report?.mark("first_frame")
        peak = max(peak, p / 32768f)
        onFrame(frame, p / 32768f)
    }

    /** What Android actually gave us: the source it used, the input device, and the effects
     *  (noise suppression, gain) running on it, ours and the system's. */
    private fun describe(c: AudioRecordingConfiguration) {
        val mic = report?.json()?.optJSONObject("mic") ?: JSONObject()
        val dev = c.audioDevice
        mic.put("actual_source", c.audioSource)
            .put("device", dev?.let { "${deviceName(it.type)} (${it.productName})" } ?: JSONObject.NULL)
            .put("device_rate", c.format.sampleRate)
            .put("effects", JSONArray(c.effects.map { it.name }))
            .put("client_effects", JSONArray(c.clientEffects.map { it.name }))
            .put("available", JSONArray(listOfNotNull(
                "agc".takeIf { AutomaticGainControl.isAvailable() },
                "ns".takeIf { NoiseSuppressor.isAvailable() },
                "aec".takeIf { AcousticEchoCanceler.isAvailable() })))
        report?.put("mic", mic)
    }

    companion object {
        const val FRAME = 640 // 20 ms

        fun sourceName(s: Int) = when (s) {
            MediaRecorder.AudioSource.VOICE_RECOGNITION -> "raw"
            MediaRecorder.AudioSource.VOICE_COMMUNICATION -> "call"
            MediaRecorder.AudioSource.MIC -> "standard"
            MediaRecorder.AudioSource.UNPROCESSED -> "unprocessed"
            else -> "source$s"
        }

        private fun deviceName(t: Int) = when (t) {
            AudioDeviceInfo.TYPE_BUILTIN_MIC -> "built-in mic"
            AudioDeviceInfo.TYPE_BLUETOOTH_SCO -> "bluetooth"
            AudioDeviceInfo.TYPE_BLE_HEADSET -> "bluetooth le"
            AudioDeviceInfo.TYPE_WIRED_HEADSET -> "wired headset"
            AudioDeviceInfo.TYPE_USB_HEADSET, AudioDeviceInfo.TYPE_USB_DEVICE -> "usb"
            else -> "type $t"
        }
    }
}
