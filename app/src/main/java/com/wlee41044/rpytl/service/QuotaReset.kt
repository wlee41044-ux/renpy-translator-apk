package com.wlee41044.rpytl.service

import android.app.AlarmManager
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import com.wlee41044.rpytl.MainActivity
import java.time.ZoneId
import java.time.ZonedDateTime
import java.util.Locale

/**
 * Gemini 무료 등급의 하루 한도는 태평양 시간(America/Los_Angeles) 자정에 초기화된다.
 * 한도를 다 쓰고 멈추면 그 시각 직후로 알림을 예약해, 알림을 보고 '번역 이어하기'를 누르면 된다.
 * (알람은 절전 모드에서 몇 분 늦을 수 있다. 재부팅하면 예약이 사라진다.)
 */
object QuotaReset {
    private const val CHANNEL = "quota"
    private const val NOTIF_ID = 2
    private const val MARGIN_MINUTES = 5L

    /** 다음 초기화 시각 (epoch ms) */
    fun nextResetMillis(now: ZonedDateTime = ZonedDateTime.now(ZoneId.of("America/Los_Angeles"))): Long =
        now.toLocalDate().plusDays(1).atStartOfDay(now.zone).plusMinutes(MARGIN_MINUTES).toInstant().toEpochMilli()

    /** 알림을 예약하고, 사용자에게 보여 줄 현지 시각 문구를 돌려준다. */
    fun schedule(ctx: Context): String {
        val at = nextResetMillis()
        val am = ctx.getSystemService(AlarmManager::class.java)
        am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, pending(ctx))
        val local = java.time.Instant.ofEpochMilli(at).atZone(ZoneId.systemDefault())
        return String.format(Locale.KOREA, "%d월 %d일 %02d:%02d", local.monthValue, local.dayOfMonth, local.hour, local.minute)
    }

    fun cancel(ctx: Context) {
        ctx.getSystemService(AlarmManager::class.java).cancel(pending(ctx))
    }

    private fun pending(ctx: Context) = PendingIntent.getBroadcast(
        ctx, 0, Intent(ctx, Receiver::class.java), PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
    )

    class Receiver : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent?) {
            val nm = context.getSystemService(NotificationManager::class.java)
            if (Build.VERSION.SDK_INT >= 26) {
                nm.createNotificationChannel(NotificationChannel(CHANNEL, "한도 초기화 알림", NotificationManager.IMPORTANCE_DEFAULT))
            }
            val open = PendingIntent.getActivity(
                context, 0, Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
            val n = NotificationCompat.Builder(context, CHANNEL)
                .setSmallIcon(android.R.drawable.stat_notify_more)
                .setContentTitle("Gemini 하루 한도가 초기화됐습니다")
                .setContentText("앱을 열어 '번역 이어하기'(또는 캐릭터 분석)를 다시 시작하세요")
                .setContentIntent(open).setAutoCancel(true).build()
            runCatching { nm.notify(NOTIF_ID, n) }   // 알림 권한이 없으면 조용히 무시
        }
    }
}
