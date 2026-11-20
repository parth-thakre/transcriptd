package dev.transcriptd.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class CaretContextTest {
    @Test fun shortTextIsUntouched() {
        assertEquals("Hi Priya, ", CaretContext.before("Hi Priya, "))
        assertEquals(" and then", CaretContext.after(" and then"))
    }

    @Test fun beforeKeepsTheEndAndStartsAtAWord() {
        val t = "word ".repeat(200) + "Dear Priya,"
        val b = CaretContext.before(t)
        assertTrue(b.length <= CaretContext.BEFORE_MAX)
        assertTrue(b.endsWith("Dear Priya,"))
        assertTrue(b.startsWith("word "))
    }

    @Test fun beforeKeepsTrailingSpaceForSpacing() {
        val t = "x".repeat(1000) + " end. "
        assertTrue(CaretContext.before(t).endsWith("end. "))
    }

    @Test fun afterKeepsTheStartAndEndsAtAWord() {
        val t = "see you there " + "later ".repeat(100)
        val a = CaretContext.after(t)
        assertTrue(a.length <= CaretContext.AFTER_MAX)
        assertTrue(a.startsWith("see you there"))
        assertTrue(a.endsWith("later"))
    }

    @Test fun noSpacesFallsBackToAHardCut() {
        val t = "字".repeat(1000)
        assertEquals(CaretContext.BEFORE_MAX, CaretContext.before(t).length)
        assertEquals(CaretContext.AFTER_MAX, CaretContext.after(t).length)
    }
}
