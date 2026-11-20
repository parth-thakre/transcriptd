package dev.transcriptd.app

import org.junit.Assert.assertFalse
import org.junit.Test

class ApiTest {
    /** A redirect hop isn't checked against ServerUrl, and 307/308 resend the body and token.
     *  The dictation WebSocket is opened on this same client (Dictation.kt). */
    @Test fun serverClientNeverFollowsRedirects() {
        assertFalse(Api.http.followRedirects)
        assertFalse(Api.http.followSslRedirects)
    }
}
