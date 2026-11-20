package dev.transcriptd.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.IBinder

/** A foreground service that exists only while you dictate: Android shows its microphone
 *  indicator and lets the bubble keep recording over another app. */
class MicService : Service() {
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Dictation", NotificationManager.IMPORTANCE_LOW))
        val n = Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_mic)
            .setContentTitle("transcriptd is listening")
            .setOngoing(true)
            .build()
        try {
            startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        } catch (_: Exception) {
            stopSelf()
        }
        return START_NOT_STICKY
    }

    companion object {
        private const val CHANNEL = "dictation"
        fun start(context: Context) {
            try { context.startForegroundService(Intent(context, MicService::class.java)) } catch (_: Exception) {}
        }
        fun stop(context: Context) {
            context.stopService(Intent(context, MicService::class.java))
        }
    }
}
