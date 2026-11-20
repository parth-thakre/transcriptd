package dev.transcriptd.app

/** Picks the server's cleanup style ([cleanup.styles]) for the app you are typing in, when the
 *  style setting is "By app". Anything not listed gets plain cleanup ("auto"). */
object Styles {
    private val message = setOf(
        "com.whatsapp", "com.whatsapp.w4b", "org.telegram.messenger", "org.thunderdog.challegram",
        "com.google.android.apps.messaging", "com.samsung.android.messaging", "com.discord",
        "com.Slack", "org.thoughtcrime.securesms", "com.instagram.android", "com.facebook.orca",
        "com.snapchat.android", "com.google.android.apps.dynamite", "com.microsoft.teams",
    )
    private val email = setOf(
        "com.google.android.gm", "com.microsoft.office.outlook", "com.samsung.android.email.provider",
        "ch.protonmail.android", "com.fsck.k9", "net.thunderbird.android",
    )
    private val note = setOf(
        "com.google.android.keep", "com.samsung.android.app.notes", "md.obsidian", "notion.id",
        "com.microsoft.office.onenote", "com.todoist", "com.google.android.apps.tasks",
    )

    fun forApp(prefs: Prefs, app: String?): String {
        if (prefs.style != "app") return prefs.style
        return when (app) {
            in message -> "message"
            in email -> "email"
            in note -> "note"
            else -> "auto"
        }
    }
}
