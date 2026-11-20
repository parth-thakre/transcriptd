package dev.transcriptd.app

import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ServerUrlTest {
    @Test fun ownNetworkHostsMayUseHttp() {
        for (h in listOf("127.0.0.1", "localhost", "10.0.2.2", "172.16.0.5", "172.31.255.1", "192.168.1.20",
            "169.254.3.4", "100.64.0.1", "100.127.255.254",
            "::1", "fe80::1", "fd7a:115c:a1e0::1", "fc00::5", "::ffff:192.168.0.1")) {
            assertTrue(h, ServerUrl.cleartextOk(h))
        }
    }

    @Test fun otherHostsMayNot() {
        for (h in listOf("8.8.8.8", "100.63.255.255", "100.128.0.1", "172.32.0.1", "192.169.0.1", "11.0.0.1",
            "example.com", "desktop.tail1234.ts.net", "DESKTOP.TAIL1234.TS.NET.", "ts.net.example.com", "myts.net", "2001:4860:4860::8888", "::ffff:8.8.8.8",
            "localhost.example.com", "1.2.3.4.5", "300.1.1.1")) {
            assertFalse(h, ServerUrl.cleartextOk(h))
        }
    }

    @Test fun problemIsNullForHttpsAnywhereAndHttpAtHome() {
        assertNull(ServerUrl.problem("https://transcribe.example.com"))
        assertNull(ServerUrl.problem("http://100.64.0.1:7411"))
        assertNull(ServerUrl.problem("100.64.0.1:7411")) // no scheme means http
        assertNull(ServerUrl.problem("http://[fd7a:115c:a1e0::1]:7411/"))
    }

    @Test fun problemExplainsPlainHttpToTheInternet() {
        val p = ServerUrl.problem("http://transcribe.example.com:7411")
        assertNotNull(p)
        assertTrue(p!!.contains("https://transcribe.example.com:7411"))
        assertNotNull(ServerUrl.problem("transcribe.example.com"))
    }

    @Test fun magicDnsNamesNeedHttps() {
        // A spoofed DNS answer (Tailscale off, hostile Wi-Fi) mustn't get the token in the clear.
        val p = ServerUrl.problem("http://desktop.tail1234.ts.net:7411")
        assertNotNull(p)
        assertTrue(p!!.contains("https://desktop.tail1234.ts.net:7411"))
        assertNull(ServerUrl.problem("https://desktop.tail1234.ts.net"))
    }

    @Test fun garbageIsAProblem() {
        assertNotNull(ServerUrl.problem("ftp://x"))
        assertNotNull(ServerUrl.problem(""))
    }
}
