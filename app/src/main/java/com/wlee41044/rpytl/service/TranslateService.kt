package com.wlee41044.rpytl.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.Uri
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.os.SystemClock
import android.provider.OpenableColumns
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import com.wlee41044.rpytl.Engine
import com.wlee41044.rpytl.MainActivity
import com.wlee41044.rpytl.Prefs
import com.wlee41044.rpytl.PyControl
import com.wlee41044.rpytl.R
import com.wlee41044.rpytl.llm.LocalLlm
import com.wlee41044.rpytl.saf.GameFolder
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicInteger

/**
 * 스캔/번역/출력 작업을 순서대로 처리하는 Foreground Service.
 * 진행 상태는 전부 DB 에 저장되므로 서비스가 죽어도 '번역 시작'을 다시 누르면 이어서 한다.
 */
class TranslateService : Service() {

    companion object {
        const val ACTION_SCAN = "scan"
        const val ACTION_TRANSLATE = "translate"
        const val ACTION_EXPORT = "export"
        const val ACTION_PAUSE = "pause"
        const val EXTRA_GAME = "game"
        const val EXTRA_RETRY_REVIEW = "retry_review"
        private const val CHANNEL = "jobs"
        private const val NOTIF_ID = 1

        fun start(ctx: Context, action: String, gameId: Long, retryReview: Boolean = false) {
            if (action != ACTION_PAUSE) JobState.stopRequested.set(false)
            val i = Intent(ctx, TranslateService::class.java)
                .setAction(action).putExtra(EXTRA_GAME, gameId).putExtra(EXTRA_RETRY_REVIEW, retryReview)
            ContextCompat.startForegroundService(ctx, i)
        }

        fun pause(ctx: Context) {
            JobState.stopRequested.set(true)
            JobState.log("일시정지 요청 — 현재 배치가 끝나면 멈춥니다")
        }
    }

    private val executor = Executors.newSingleThreadExecutor()
    private val pending = AtomicInteger(0)
    private lateinit var engine: Engine
    private lateinit var prefs: Prefs
    private var wakeLock: PowerManager.WakeLock? = null
    private var lastNotify = 0L

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        engine = Engine(this)
        prefs = Prefs(this)
        val nm = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "번역 작업", NotificationManager.IMPORTANCE_LOW))
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        try {
            ServiceCompat.startForeground(
                this, NOTIF_ID, notification("준비 중", 0, 0),
                if (Build.VERSION.SDK_INT >= 34) ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE else 0,
            )
        } catch (e: Exception) {
            // 백그라운드 재시작 등으로 포그라운드 시작이 거부되면 그냥 종료 (DB 기준으로 나중에 이어하기)
            JobState.log("서비스 시작 실패: ${e.message}")
            stopSelf(startId)
            return START_NOT_STICKY
        }
        val action = intent?.action
        val gameId = intent?.getLongExtra(EXTRA_GAME, -1L) ?: -1L
        if (action == null || gameId < 0 || action == ACTION_PAUSE) {
            if (pending.get() == 0) stopSelfSafely()
            return START_NOT_STICKY
        }
        val retry = intent.getBooleanExtra(EXTRA_RETRY_REVIEW, false)
        pending.incrementAndGet()
        executor.execute {
            try {
                runJob(action, gameId, retry)
            } catch (t: Throwable) {
                JobState.log("오류: ${t.javaClass.simpleName}: ${t.message}")
                android.util.Log.e("rpytl", "job failed", t)
            } finally {
                JobState.setRunning(null)
                JobState.dbChanged()
                if (pending.decrementAndGet() == 0) stopSelfSafely()
            }
        }
        // 시스템이 죽였다가 살리면 같은 작업을 다시 받는다 (번역은 DB 기준 이어하기)
        return START_REDELIVER_INTENT
    }

    private fun stopSelfSafely() {
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    override fun onDestroy() {
        JobState.stopRequested.set(true)
        executor.shutdown()
        releaseWakeLock()
        super.onDestroy()
    }

    private fun acquireWakeLock() {
        if (wakeLock?.isHeld == true) return
        val pm = getSystemService(PowerManager::class.java)
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "rpytl:job").apply {
            setReferenceCounted(false)
            acquire(12 * 60 * 60 * 1000L)
        }
    }

    private fun releaseWakeLock() {
        runCatching { wakeLock?.takeIf { it.isHeld }?.release() }
        wakeLock = null
    }

    private fun gameInfo(gameId: Long) = engine.listGames().firstOrNull { it.id == gameId }
        ?: throw IllegalStateException("게임 $gameId 없음")

    private fun runJob(action: String, gameId: Long, retryReview: Boolean) {
        val game = gameInfo(gameId)
        JobState.setRunning(JobState.Running(gameId, action))
        acquireWakeLock()
        try {
            when (action) {
                ACTION_SCAN -> scan(game.id, Uri.parse(game.folderUri), game.language)
                ACTION_TRANSLATE -> translate(game.id, game.name, retryReview)
                ACTION_EXPORT -> export(game.id, Uri.parse(game.folderUri), game.language)
            }
        } finally {
            releaseWakeLock()
        }
    }

    private fun scan(gameId: Long, uri: Uri, language: String) {
        JobState.log("스캔 시작")
        updateNotification("스캔 중", 0, 0)
        val folder = GameFolder(contentResolver, uri)
        val n = folder.importScripts(engine, gameId, language) { JobState.log(it) }
        if (n == 0) {
            JobState.log("스크립트(.rpa/.rpy/.rpyc)를 찾지 못했습니다. game 폴더 또는 그 상위 폴더를 골랐는지 확인하세요.")
            return
        }
        val r = engine.scan(gameId, folder.renpyVersionText())
        val c = r.getJSONObject("counts")
        JobState.log(
            "스캔 완료 (Ren'Py ${r.optString("renpy_version", "?")}): 항목 ${r.getInt("items")}개 — " +
                "신규 ${c.getInt("added")}, 변경 ${c.getInt("changed")}, 삭제(미사용) ${c.getInt("removed")}, " +
                "유지 ${c.getInt("unchanged")}, 재사용 ${c.getInt("reactivated")}"
        )
        val errors = r.getJSONArray("errors")
        for (i in 0 until minOf(errors.length(), 30)) JobState.log("  읽기 실패: ${errors.getString(i)}")
        if (errors.length() > 30) JobState.log("  ... 외 ${errors.length() - 30}개")
    }

    private fun translate(gameId: Long, name: String, retryReview: Boolean) {
        val engineName = prefs.engine
        val opts = prefs.translateOpts(engineName, retryReview)
        val needLocal = engineName == "local" || opts.has("fallback")
        val local = if (needLocal) LocalLlm.create(this, prefs) { JobState.log(it) } else null
        val control = object : PyControl {
            override fun isStopped(): Boolean = JobState.stopRequested.get()
            override fun log(msg: String) = JobState.log(msg)
            override fun progress(done: Int, total: Int) {
                JobState.setProgress(gameId, done, total)
                JobState.dbChanged()
                updateNotification("$name 번역 중", done, total)
            }
        }
        try {
            val r = engine.translate(gameId, engineName, opts, control, local)
            if (r.optString("status") == "error") JobState.log("중단 사유: ${r.optString("message")}")
        } finally {
            local?.close()
        }
    }

    private fun export(gameId: Long, uri: Uri, language: String) {
        JobState.log("출력 파일 생성 시작")
        updateNotification("출력 파일 생성 중", 0, 0)
        val folder = GameFolder(contentResolver, uri)
        val fontUri = prefs.fontUri(gameId)
        var fontName: String? = null
        if (fontUri != null) {
            val display = runCatching {
                contentResolver.query(Uri.parse(fontUri), arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)
                    ?.use { c -> if (c.moveToFirst()) c.getString(0) else null }
            }.getOrNull() ?: ""
            val ext = if (display.lowercase().endsWith(".otf")) "otf" else "ttf"
            val fname = "font.$ext"
            try {
                folder.writeFont(language, fname, Uri.parse(fontUri))
                fontName = fname
                JobState.log("폰트 복사: tl/$language/rpytl/$fname")
            } catch (e: Exception) {
                JobState.log("폰트 복사 실패: ${e.message}")
            }
        }
        val existingTl = java.io.File(engine.workDir(gameId), "tl/$language").absolutePath
        val r = engine.export(gameId, fontName?.let { "tl/$language/rpytl/$it" }, prefs.forceLanguage, existingTl)
        val info = r.getJSONObject("info")
        val n = folder.writeOutput(engine.outDir(gameId), r, fontName) { JobState.log(it) }
        JobState.log(
            "출력 완료: 파일 $n 개 → game/tl/$language/rpytl/ (대사 파일 ${info.getInt("dialogue_files")}개, " +
                "문자열 ${info.getInt("strings")}개, 기존 번역과 겹쳐 제외 ${info.getInt("skipped_existing")}개)"
        )
    }

    private fun updateNotification(text: String, done: Int, total: Int) {
        val now = SystemClock.elapsedRealtime()
        if (now - lastNotify < 1000 && total > 0 && done < total) return
        lastNotify = now
        runCatching {
            getSystemService(NotificationManager::class.java).notify(NOTIF_ID, notification(text, done, total))
        }
    }

    private fun notification(text: String, done: Int, total: Int): Notification {
        val pi = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle("RenPy 번역기")
            .setContentText(if (total > 0) "$text $done/$total" else text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(pi)
            .apply { if (total > 0) setProgress(total, done, false) }
            .build()
    }
}
