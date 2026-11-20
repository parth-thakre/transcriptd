package dev.transcriptd.app

import android.content.Context
import android.os.VibrationEffect
import android.os.VibratorManager

object Haptics {
    private fun play(context: Context, effect: VibrationEffect) {
        if (!Prefs(context).haptics) return
        context.getSystemService(VibratorManager::class.java)?.defaultVibrator?.vibrate(effect)
    }

    fun start(c: Context) = play(c, VibrationEffect.createPredefined(VibrationEffect.EFFECT_HEAVY_CLICK))
    fun tick(c: Context) = play(c, VibrationEffect.createPredefined(VibrationEffect.EFFECT_TICK))
    fun done(c: Context) = play(c, VibrationEffect.createPredefined(VibrationEffect.EFFECT_CLICK))
    fun nothing(c: Context) = play(c, VibrationEffect.createPredefined(VibrationEffect.EFFECT_DOUBLE_CLICK))
}
