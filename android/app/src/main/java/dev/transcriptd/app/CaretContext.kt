package dev.transcriptd.app

/** How much of the field around the caret goes to the server with a take, as `context`
 *  (server/transcriptd/context.py). The server uses it for three things: the leading space and
 *  first-letter case (only the last few characters before the caret and the first one after),
 *  names already in the field as vocabulary, and whether the field is mostly Devanagari (the last
 *  400 characters before the caret and the first 100 after). So at most that much is sent, cut
 *  back to a whole word. The end of `before` and the start of `after` are never touched: they
 *  decide the spacing. Plain Kotlin, unit-tested (CaretContextTest). */
object CaretContext {
    const val BEFORE_MAX = 400
    const val AFTER_MAX = 100

    /** At most [BEFORE_MAX] characters ending at the caret, starting at a word. */
    fun before(text: String): String {
        if (text.length <= BEFORE_MAX) return text
        var start = text.length - BEFORE_MAX
        if (start < text.length && text[start].isLowSurrogate()) start++
        // A word cut in half at the start: drop it, unless that would leave only spaces.
        if (!text[start - 1].isWhitespace()) {
            val ws = (start until text.length).firstOrNull { text[it].isWhitespace() }
            if (ws != null && text.substring(ws + 1).isNotBlank()) start = ws + 1
        }
        return text.substring(start)
    }

    /** At most [AFTER_MAX] characters starting at the caret, ending at a word. */
    fun after(text: String): String {
        if (text.length <= AFTER_MAX) return text
        var end = AFTER_MAX
        if (text[end - 1].isHighSurrogate()) end--
        if (!text[end].isWhitespace()) {
            val ws = (end - 1 downTo 1).firstOrNull { text[it].isWhitespace() }
            if (ws != null && text.substring(0, ws).isNotBlank()) end = ws
        }
        return text.substring(0, end)
    }
}
