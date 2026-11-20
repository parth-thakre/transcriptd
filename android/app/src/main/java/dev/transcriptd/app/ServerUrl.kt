package dev.transcriptd.app

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

/** Which server addresses the app will talk to. Android's network security config can't name IP
 *  ranges, so cleartext is allowed app-wide (res/xml/network_security_config.xml) and limited
 *  here instead: plain http:// only to hosts on your own network, where the link is either local
 *  or already encrypted (Tailscale's WireGuard). Anything else has to be https://. Plain Kotlin,
 *  unit-tested (ServerUrlTest). */
object ServerUrl {
    /** The address as typed or pasted, with http:// added when it has no scheme. Null when it
     *  isn't an http(s) URL at all. */
    fun parse(server: String): HttpUrl? {
        val s = server.trim().trimEnd('/')
        if (s.isEmpty()) return null
        return (if (s.startsWith("http://") || s.startsWith("https://")) s else "http://$s").toHttpUrlOrNull()
    }

    /** Why the app won't use this address, or null when it's fine. */
    fun problem(server: String): String? {
        val url = parse(server) ?: return "“$server” isn't a server address. Use something like https://your-server.example.ts.net."
        if (url.isHttps || cleartextOk(url.host)) return null
        return "Plain http:// is only for IP addresses on your own network (Tailscale's 100.x, your LAN or this phone). " +
            "Use https://${url.host}${if (url.port != 80) ":${url.port}" else ""} instead."
    }

    /** Loopback, RFC 1918 private, link-local, Tailscale's CGNAT range (100.64.0.0/10), IPv6
     *  loopback, link-local and unique-local (fc00::/7, which holds Tailscale's
     *  fd7a:115c:a1e0::/48). Other names are refused, MagicDNS's *.ts.net included: what they
     *  resolve to can't be pinned down before connecting (with Tailscale off, any DNS server can
     *  answer for them), and `tailscale serve` gives them https:// anyway. */
    fun cleartextOk(host: String): Boolean {
        val h = host.lowercase().trimEnd('.').removePrefix("[").removeSuffix("]")
        if (h == "localhost" || h.endsWith(".localhost")) return true
        ipv4(h)?.let { b ->
            val (a, c) = b[0] to b[1]
            return a == 127 || a == 10 || (a == 172 && c in 16..31) || (a == 192 && c == 168) ||
                (a == 169 && c == 254) || (a == 100 && c in 64..127)
        }
        ipv6(h)?.let { g ->
            if (g.take(7).all { it == 0 } && g[7] == 1) return true // ::1
            if ((g[0] and 0xffc0) == 0xfe80) return true                // fe80::/10 link-local
            if ((g[0] and 0xfe00) == 0xfc00) return true                // fc00::/7 unique local
            // ::ffff:a.b.c.d, an IPv4 address written as IPv6.
            if (g.take(5).all { it == 0 } && g[5] == 0xffff) {
                return cleartextOk("${g[6] shr 8}.${g[6] and 0xff}.${g[7] shr 8}.${g[7] and 0xff}")
            }
            return false
        }
        return false
    }

    /** Four octets of a dotted IPv4 literal, else null. */
    private fun ipv4(h: String): IntArray? {
        val parts = h.split('.')
        if (parts.size != 4) return null
        val out = IntArray(4)
        for ((i, p) in parts.withIndex()) {
            if (p.isEmpty() || p.length > 3 || !p.all { it in '0'..'9' }) return null
            out[i] = p.toInt().takeIf { it <= 255 } ?: return null
        }
        return out
    }

    /** The eight 16-bit groups of an IPv6 literal (with :: and a trailing dotted IPv4), else null. */
    private fun ipv6(h: String): IntArray? {
        if (':' !in h) return null
        val addr = h.substringBefore('%') // zone id, as in fe80::1%wlan0
        fun groups(s: String): List<Int>? {
            if (s.isEmpty()) return emptyList()
            val out = ArrayList<Int>()
            for (p in s.split(':')) {
                if ('.' in p) {
                    val v4 = ipv4(p) ?: return null
                    out += (v4[0] shl 8) or v4[1]; out += (v4[2] shl 8) or v4[3]
                } else {
                    if (p.isEmpty() || p.length > 4) return null
                    out += p.toIntOrNull(16) ?: return null
                }
            }
            return out
        }
        val halves = addr.split("::")
        if (halves.size > 2) return null
        val head = groups(halves[0]) ?: return null
        val tail = if (halves.size == 2) groups(halves[1]) ?: return null else emptyList()
        val fill = 8 - head.size - tail.size
        if (fill < 0 || (halves.size == 1 && fill != 0) || (halves.size == 2 && fill < 1)) return null
        return (head + List(fill) { 0 } + tail).toIntArray()
    }
}
