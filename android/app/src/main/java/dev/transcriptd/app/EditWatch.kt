package dev.transcriptd.app

/** What happened to one dictation after it went in: FlowService feeds it every reading of the
 *  field's text and tells it when focus moved, and this decides when the watch is over and what
 *  to send to `POST /v1/dictations/{id}/edited`. Only runs with "Learn from my edits" on (Prefs).
 *  Plain Kotlin (no Android), so it's unit-tested.
 *
 *  It keeps the last NON-EMPTY text: chat apps clear the field when the message is sent, and the
 *  text just before that is the one that went out. What's sent is only a window around the
 *  dictation ([window], at most [MAX_SENT] characters; a short field fits in it whole), and
 *  nothing when the dictation was never seen in the field or can't be placed in it. */
class EditWatch(
    val dictationId: Int,
    /** Exactly what the app inserted (insert_text, with its leading space if any). */
    val inserted: String,
    val app: String?,
    val startedAt: Long,
    val timeoutMs: Long = TIMEOUT_MS,
) {
    enum class Reason(val wire: String) { SENT("sent"), FOCUS_LOST("focus_lost"), REPLACED("replaced"), TIMEOUT("timeout") }

    /** How it ended. [send] says whether to post at all; [finalText] is what goes out as
     *  `final_text`: the window of the field around the dictation, or null when [changed] is false
     *  (around the dictation, the field still holds what it held right after it went in). */
    class Outcome(val reason: Reason, val finalText: String?, val changed: Boolean, val send: Boolean)

    /** The part of the field to send, and whether it differs from what was there after the insertion. */
    class Window(val text: String, val changed: Boolean)

    private val needle = inserted.trim()
    /** The first text seen with the dictation in it: what "unchanged" means. */
    var baseline: String? = null
        private set
    var last: String? = null
        private set
    private var peak = 0
    var outcome: Outcome? = null
        private set
    val done get() = outcome != null

    /** A reading of the field ("" when it's empty or showing its hint). Returns the outcome when
     *  this reading ends the watch: the field was cleared after having text, i.e. sent. */
    fun text(t: String): Outcome? {
        if (done) return null
        if (t.isEmpty()) {
            val l = last ?: return null
            // Backspaced down to nothing (the last text seen was a few characters, after more):
            // you're redoing it by hand, not sending it. Keep watching what you type next.
            if (l.length <= MANUAL_CLEAR_MAX && peak > l.length) { last = null; peak = 0; return null }
            return finish(Reason.SENT)
        }
        if (baseline == null && needle.isNotEmpty() && t.contains(needle)) baseline = t
        last = t
        if (t.length > peak) peak = t.length
        return null
    }

    /** Ends it once the time is up. */
    fun tick(now: Long): Outcome? = if (!done && now - startedAt >= timeoutMs) finish(Reason.TIMEOUT) else null

    /** Ends it for [reason]; null when it had already ended. */
    fun finish(reason: Reason): Outcome? {
        if (done) return null
        val final = last
        // Unchanged or edited, only when the dictation was seen in the field (that's where it went
        // in, and where it is) and can be placed in what the field holds now. Never seen: whatever
        // is in the field can't be tied to the dictation, so nothing goes, even when it was sent.
        val base = baseline
        val win = if (final != null && base != null) window(base, needle, final) else null
        val changed = win?.changed == true
        return Outcome(reason, if (changed) win!!.text else null, changed, win != null).also { outcome = it }
    }

    companion object {
        const val TIMEOUT_MS = 90_000L
        const val MANUAL_CLEAR_MAX = 3
        /** Characters of the field kept on either side of the dictation's region. */
        const val MARGIN = 200
        /** The most `final_text` ever holds (the server's limit too). */
        const val MAX_SENT = 4000

        /** The dictation's region in [final] with up to [MARGIN] characters either side, cut at
         *  word boundaries, at most [MAX_SENT] characters. The region is found from [baseline] (the
         *  field right after the insertion, holding [needle]): what [final] shares with it at the
         *  start and the end is untouched, the rest is your edit, and the region is the dictation
         *  widened to whatever of the edit overlaps it. Null when it can't be placed: the dictation
         *  isn't in [baseline]. Without a baseline there is nothing to place it by, so callers
         *  send nothing (never the field as it is). */
        fun window(baseline: String, needle: String, final: String): Window? {
            val at = baseline.indexOf(needle)
            if (at < 0) return null
            val b = baseline.length
            val f = final.length
            val lim = minOf(b, f)
            var p = 0 // shared start
            while (p < lim && baseline[p] == final[p]) p++
            var s = 0 // shared end, not overlapping the shared start
            while (s < lim - p && baseline[b - 1 - s] == final[f - 1 - s]) s++
            val shift = f - b
            val endB = at + needle.length
            // Start and end of the region in final. Text typed right at either edge counts as part of it.
            val start = when { at <= p -> at; at >= b - s -> at + shift; else -> p }
            val end = when { endB >= b - s -> endB + shift; endB <= p -> endB; else -> f - s }
            val margin = minOf(MARGIN, (MAX_SENT - (end - start)) / 2)
            if (margin < 0) return null
            val ws = wordStart(final, maxOf(0, start - margin), start)
            val we = wordEnd(final, minOf(f, end + margin), end)
            // The edit is final[p, f - s); none of it in the window means it's as it went in.
            val changed = final != baseline && p <= we && f - s >= ws
            return Window(final.substring(ws, we), changed)
        }

        /** [from] moved forward to the start of a word, not past [limit]. */
        private fun wordStart(t: String, from: Int, limit: Int): Int {
            if (from <= 0 || from >= t.length || t[from - 1].isWhitespace()) return from
            for (i in from until limit) if (t[i].isWhitespace()) return i + 1
            return if (t[from].isLowSurrogate()) from + 1 else from
        }

        /** [from] moved back to the end of a word, not before [limit]. */
        private fun wordEnd(t: String, from: Int, limit: Int): Int {
            if (from <= 0 || from >= t.length || t[from].isWhitespace()) return from
            for (i in from - 1 downTo limit) if (t[i].isWhitespace()) return i
            return if (t[from - 1].isHighSurrogate()) from - 1 else from
        }
    }
}
