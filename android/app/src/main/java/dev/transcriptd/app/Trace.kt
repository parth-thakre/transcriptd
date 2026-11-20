package dev.transcriptd.app

import org.json.JSONObject

/** The last dictation, for the playground: the server's full result (what the speech model
 *  heard, every cleanup edit and whether the guard kept it, timings, audio levels), how long it
 *  took from letting go to the text appearing, how it went in, and the telemetry report sent for
 *  it. Main thread only. */
object Trace {
    class Entry(
        val result: JSONObject?,  // null when it never reached the server
        val roundTripMs: Long,
        val outcome: String,       // "typed in", "set the field", "pasted", "copied", "nothing heard", "not sent"
        val error: String? = null,
        val report: JSONObject? = null,
    ) {
        /** What you did with it afterwards (EditWatch), once that's known. */
        var edit: EditWatch.Outcome? = null
    }

    var last: Entry? = null
        private set
    var listener: (() -> Unit)? = null

    /** The sentence the playground asks you to read out, while it's open; FlowService puts it in
     *  the report of takes dictated into the playground, so the server can score them. */
    var expected: String? = null

    /** The edit watch on a dictation ended; shown on the playground if it's the last take. */
    fun edited(dictationId: Int, out: EditWatch.Outcome) {
        val e = last ?: return
        if (e.result?.optInt("dictation_id", -1) != dictationId) return
        e.edit = out
        listener?.invoke()
    }

    fun record(e: Entry) {
        last = e
        listener?.invoke()
    }
}
