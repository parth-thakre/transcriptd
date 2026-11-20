package dev.transcriptd.app

import dev.transcriptd.app.EditWatch.Reason
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class EditWatchTest {
    private fun watch(inserted: String = "see you at 7") = EditWatch(42, inserted, "com.whatsapp", startedAt = 1_000)

    @Test fun clearedAfterTextIsSentWithTheTextBeforeIt() {
        val w = watch()
        assertNull(w.text("see you at 7"))
        assertNull(w.text("see you at 7 tonight"))
        val out = w.text("")
        assertNotNull(out)
        assertEquals(Reason.SENT, out!!.reason)
        assertEquals("see you at 7 tonight", out.finalText)
        assertTrue(out.changed)
        assertTrue(out.send)
    }

    @Test fun unchangedAndSentIsSentAsIs() {
        val w = watch()
        w.text("see you at 7")
        val out = w.text("")!!
        assertFalse(out.changed)
        assertTrue(out.send)
    }

    @Test fun emptyBeforeAnyTextDoesNotFinish() {
        val w = watch()
        assertNull(w.text("")) // a stale read from before the insertion landed
        assertFalse(w.done)
        w.text("see you at 7")
        assertEquals(Reason.SENT, w.text("")!!.reason)
    }

    @Test fun backspacingToNothingIsNotSending() {
        val w = watch()
        w.text("see you at 7")
        w.text("se")
        w.text("s")
        assertNull(w.text(""))
        assertFalse(w.done)
        w.text("Running late")
        val out = w.text("")!!
        assertEquals(Reason.SENT, out.reason)
        assertEquals("Running late", out.finalText)
        assertTrue(out.changed)
    }

    @Test fun shortDictationSentAsIsIsStillSent() {
        val w = watch("OK")
        w.text("OK")
        assertEquals(Reason.SENT, w.text("")!!.reason)
    }

    @Test fun focusLostKeepsTheLastText() {
        val w = watch(" see you at 7")
        w.text("Sure, see you at 7")
        w.text("Sure, see you at 8")
        val out = w.finish(Reason.FOCUS_LOST)!!
        assertEquals(Reason.FOCUS_LOST, out.reason)
        assertEquals("Sure, see you at 8", out.finalText)
        assertTrue(out.changed)
        assertTrue(out.send)
    }

    @Test fun unchangedOnFocusLostIsSentWhenTheDictationIsInTheField() {
        val out = watch().apply { text("Sure, see you at 7") }.finish(Reason.FOCUS_LOST)!!
        assertFalse(out.changed)
        assertTrue(out.send)
    }

    @Test fun neverSawTheDictationAndNotSentSendsNothing() {
        val w = watch()
        w.text("something else entirely")
        val out = w.finish(Reason.REPLACED)!!
        assertFalse(out.send)
    }

    @Test fun neverSawTheDictationButSentSendsNothing() {
        // A short, unrelated message sent from the field is not the dictation's edit.
        val w = watch()
        w.text("something else entirely")
        val out = w.text("")!!
        assertEquals(Reason.SENT, out.reason)
        assertFalse(out.send)
        assertNull(out.finalText)
    }

    @Test fun neverSawTheDictationInAShortFieldSendsNothingOnAnyEnding() {
        for (r in listOf(Reason.FOCUS_LOST, Reason.TIMEOUT, Reason.REPLACED)) {
            val w = watch()
            w.text("see you at")
            w.text("see you at 8")
            val out = w.finish(r)!!
            assertFalse(r.wire, out.send)
            assertNull(r.wire, out.finalText)
        }
    }

    @Test fun windowWithTheDictationGoneFromTheBaselineIsNull() {
        assertNull(EditWatch.window("Sure, see you", "see you at 7", "Sure, see you at 8"))
    }

    @Test fun nothingSeenSendsNothing() {
        val out = watch().finish(Reason.TIMEOUT)!!
        assertNull(out.finalText)
        assertFalse(out.send)
        assertFalse(out.changed)
    }

    @Test fun finishesOnlyOnce() {
        val w = watch()
        w.text("see you at 7")
        assertNotNull(w.text(""))
        assertNull(w.finish(Reason.FOCUS_LOST))
        assertNull(w.text("more"))
        assertEquals(Reason.SENT, w.outcome!!.reason)
    }

    @Test fun timesOutAfter90Seconds() {
        val w = watch()
        w.text("see you at 7")
        assertNull(w.tick(1_000 + 89_999))
        val out = w.tick(1_000 + 90_000)!!
        assertEquals(Reason.TIMEOUT, out.reason)
        assertTrue(out.send)
        assertNull(w.tick(1_000 + 120_000))
    }

    @Test fun baselineIsTheFirstTextWithTheDictationIn() {
        val w = watch(" see you at 7")
        w.text("Sure,") // before the insertion landed
        w.text("Sure, see you at 7")
        assertEquals("Sure, see you at 7", w.baseline)
        assertFalse(w.finish(Reason.TIMEOUT)!!.changed)
    }

    // -- what's sent: at most MAX_SENT characters around the dictation --------------------

    @Test fun unchangedSendsNullFinalText() {
        val w = watch()
        w.text("Sure, see you at 7")
        val out = w.finish(Reason.FOCUS_LOST)!!
        assertTrue(out.send)
        assertFalse(out.changed)
        assertNull(out.finalText)
    }

    @Test fun editedSendsAWindowWithTheEdit() {
        val w = watch(" see you at 7")
        w.text("Sure, see you at 7")
        w.text("Sure, see you at 8 tonight")
        val out = w.finish(Reason.FOCUS_LOST)!!
        assertTrue(out.send)
        assertTrue(out.changed)
        assertTrue(out.finalText!!.length <= EditWatch.MAX_SENT)
        assertTrue(out.finalText!!.contains("see you at 8 tonight"))
    }

    @Test fun longFieldIsTrimmedToTheDictationAndItsMargins() {
        val head = "alpha ".repeat(1000) // 6,000 characters before
        val tail = " omega".repeat(1000) // and after
        val w = watch("see you at 7")
        w.text(head + "see you at 7" + tail)
        val out = w.text(head + "see you at 8" + tail)
        assertNull(out)
        val done = w.finish(Reason.FOCUS_LOST)!!
        val sent = done.finalText!!
        assertTrue(done.send)
        assertTrue(sent.length <= 12 + 2 * EditWatch.MARGIN)
        assertTrue(sent.contains("see you at 8"))
        // Cut at words: whole "alpha"s and "omega"s at either end.
        assertTrue(sent.startsWith("alpha "))
        assertTrue(sent.endsWith(" omega"))
    }

    @Test fun windowNeverExceedsTheLimit() {
        val long = "word ".repeat(1500).trim() // a 7,500-character dictation
        val w = watch(long)
        w.text("x ".repeat(500) + long)
        w.text("x ".repeat(500) + long + " and more")
        val out = w.finish(Reason.FOCUS_LOST)!!
        // The dictation alone is over the limit: nothing goes rather than more than MAX_SENT.
        assertFalse(out.send)
        assertNull(out.finalText)
    }

    @Test fun anEditFarFromTheDictationIsKeptAsIs() {
        val w = watch("see you at 7")
        val rest = "lorem ".repeat(200)
        w.text("see you at 7 " + rest)
        w.text("see you at 7 " + rest + "PS bring snacks")
        val out = w.finish(Reason.FOCUS_LOST)!!
        assertTrue(out.send)
        assertFalse(out.changed)
        assertNull(out.finalText)
    }

    @Test fun notLocatedInALongFieldIsNotSent() {
        val w = watch()
        w.text("something else entirely. ".repeat(100))
        val out = w.text("")!!
        assertEquals(Reason.SENT, out.reason)
        assertFalse(out.send)
        assertNull(out.finalText)
    }

    @Test fun deletedDictationSendsTheWindowAroundWhereItWas() {
        val w = watch(" see you at 7")
        w.text("Sure, see you at 7. Bye")
        w.text("Sure. Bye")
        val out = w.finish(Reason.FOCUS_LOST)!!
        assertTrue(out.send)
        assertTrue(out.changed)
        assertEquals("Sure. Bye", out.finalText)
    }
}
