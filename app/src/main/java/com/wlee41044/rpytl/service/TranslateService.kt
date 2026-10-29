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
import com.wlee41044.rpytl.apps.AppGame
import com.wlee41044.rpytl.saf.GameFolder
import org.json.JSONObject
import com.wlee41044.rpytl.shizuku.ShizukuFs
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
        const val ACTION_ANALYZE = "analyze"
        const val ACTION_ANALYZE_TRANSLATE = "analyze_translate"
        const val ACTION_PAUSE = "pause"
        const val EXTRA_GAME = "game"
        const val EXTRA_RETRY_REVIEW = "retry_review"
        private const val CHANNEL = "jobs"
        /** games.folder_uri 가 이 접두어면 설치된 앱 게임 (apk:<패키지>) */
        const val APK_PREFIX = "apk:"
        private const val NOTIF_ID = 1

        fun start(ctx: Context, action: String, gameId: Long, retryReview: Boolean = false) {
            // 작업 중에 다른 작업을 누르면 대기열에 들어가므로, 실행 중인 작업의 일시정지 요청을 지우지 않는다
            if (action != ACTION_PAUSE && JobState.running.value == null) JobState.stopRequested.set(false)
            val i = Intent(ctx, TranslateService::class.java)
                .setAction(action).putExtra(EXTRA_GAME, gameId).putExtra(EXTRA_RETRY_REVIEW, retryReview)
            ContextCompat.startForegroundService(ctx, i)
        }

        fun pause(ctx: Context) {
            JobState.stopRequested.set(true)
            LocalLlm.cancelActive()
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

    /**
     * 작업 중 CPU 가 잠들지 않게 잡는다. 며칠씩 걸리는 번역에서도 끊기지 않도록 시간 제한(2시간)을 두고
     * 진행 상황이 올 때마다(updateNotification) 다시 잡아 연장한다. 비정상 종료 시 영원히 잡히는 것도 막는다.
     */
    private fun acquireWakeLock() {
        val wl = wakeLock ?: getSystemService(PowerManager::class.java)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "rpytl:job")
            .apply { setReferenceCounted(false) }
            .also { wakeLock = it }
        wl.acquire(2 * 60 * 60 * 1000L)   // 이미 잡혀 있으면 시간만 갱신된다 (reference counted = false)
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
        if (action != ACTION_EXPORT) engine.backup()   // 번역 기록 보호 (최근 3개 보관)
        acquireWakeLock()
        QuotaReset.cancel(this)   // 직접 다시 시작했으면 예약된 한도 초기화 알림은 필요 없다
        try {
            when (action) {
                ACTION_SCAN -> scan(game.id, game.folderUri, game.language)
                ACTION_TRANSLATE -> translate(game.id, game.name, retryReview)
                ACTION_ANALYZE -> analyze(game.id)
                ACTION_ANALYZE_TRANSLATE -> {
                    // 밤새 한 번에: 캐릭터 분석 → (로컬 모델로) 번역. 분석이 멈추면 번역은 하지 않는다.
                    if (analyze(game.id)) translate(game.id, game.name, retryReview, forceLocal = true)
                }
                ACTION_EXPORT -> export(game.id, game.folderUri, game.language)
            }
        } finally {
            releaseWakeLock()
        }
    }

    private val logControl = object : PyControl {
        override fun isStopped(): Boolean = JobState.stopRequested.get()
        override fun log(msg: String) = JobState.log(msg)
        override fun progress(done: Int, total: Int) {}
    }

    private fun scan(gameId: Long, location: String, language: String) {
        JobState.log("스캔 시작")
        updateNotification("스캔 중", 0, 0)
        val n: Int
        var versionText: String? = null
        if (location.startsWith(APK_PREFIX)) {
            n = AppGame(this, location.removePrefix(APK_PREFIX)).importScripts(engine, gameId, language, logControl)
        } else {
            val folder = GameFolder(contentResolver, Uri.parse(location))
            n = folder.importScripts(engine, gameId, language) { JobState.log(it) }
            versionText = folder.renpyVersionText()
        }
        if (n == 0) {
            JobState.log("스크립트(.rpa/.rpy/.rpyc)를 찾지 못했습니다.")
            return
        }
        val r = engine.scan(gameId, versionText, prefs.pyStrings, prefs.skipExisting)
        val c = r.getJSONObject("counts")
        JobState.log(
            "스캔 완료 (Ren'Py ${r.optString("renpy_version", "?")}): 항목 ${r.getInt("items")}개 — " +
                "신규 ${c.getInt("added")}, 변경 ${c.getInt("changed")}, 삭제(미사용) ${c.getInt("removed")}, " +
                "유지 ${c.getInt("unchanged")}, 재사용 ${c.getInt("reactivated")}" +
                (if (c.optInt("existing_translation") > 0) ", 기존 번역이 덮어서 건너뜀 ${c.getInt("existing_translation")}" else "")
        )
        val errors = r.getJSONArray("errors")
        for (i in 0 until minOf(errors.length(), 30)) JobState.log("  읽기 실패: ${errors.getString(i)}")
        if (errors.length() > 30) JobState.log("  ... 외 ${errors.length() - 30}개")
    }

    /** 캐릭터 분석 (로컬 모델). 끝까지 갔으면 true. */
    private fun analyze(gameId: Long): Boolean {
        updateNotification("캐릭터 분석 중", 0, 0)
        val useGemini = prefs.engine == "gemini"
        val local = if (useGemini) null else LocalLlm.create(this, prefs) { JobState.log(it) }
        if (!useGemini && local == null) {
            JobState.log("캐릭터 분석에는 로컬 모델 또는 Gemini 가 필요합니다. 설정에서 GGUF 모델을 지정하거나 엔진을 Gemini 로 하세요.")
            return false
        }
        if (useGemini && prefs.geminiKey.isBlank()) {
            JobState.log("Gemini API 키가 비어 있습니다 (설정에서 입력하세요)")
            return false
        }
        val control = object : PyControl {
            override fun isStopped(): Boolean = JobState.stopRequested.get()
            override fun log(msg: String) = JobState.log(msg)
            override fun progress(done: Int, total: Int) {
                updateNotification("캐릭터 분석 중", done, total)
            }
        }
        return try {
            val opts = JSONObject().put("min_lines", 5).put("max_characters", 40)
            if (useGemini) opts.put("gemini", prefs.geminiOpts())
            val r = engine.analyze(gameId, opts, control, local)
            when (r.optString("status")) {
                "error" -> JobState.log("중단 사유: ${r.optString("message")}")
                "paused" -> onPaused(r)
            }
            JobState.dbChanged()
            r.optString("status") == "finished"
        } finally {
            local?.close()
        }
    }

    private fun translate(gameId: Long, name: String, retryReview: Boolean, forceLocal: Boolean = false) {
        // "분석 후 번역(밤새)" 은 로컬 모델로 번역하되, 엔진이 Gemini 로 선택돼 있으면 Gemini 로 한다
        val engineName = if (forceLocal && prefs.engine != "gemini") "local" else prefs.engine
        if (engineName == "gemini" && prefs.geminiKey.isBlank()) {
            JobState.log("Gemini API 키가 비어 있습니다 (설정에서 입력하세요)")
            return
        }
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
            when (r.optString("status")) {
                "error" -> JobState.log("중단 사유: ${r.optString("message")}")
                "paused" -> onPaused(r)
            }
        } finally {
            local?.close()
        }
    }

    /**
     * 요청 제한(429) 연속 또는 Gemini 하루 한도 소진으로 자동 일시정지. 대기열의 다음 작업도 같은 제한에 걸리므로 같이 멈춘다.
     * 하루 한도면 초기화 시각(태평양 시간 자정)에 알림을 예약한다.
     */
    private fun onPaused(r: JSONObject) {
        JobState.stopRequested.set(true)
        if (r.optBoolean("per_day")) {
            val at = QuotaReset.schedule(this)
            JobState.log("${r.optString("message")}. 대기 중인 다음 작업도 멈춥니다. 한도가 초기화되는 $at 쯤 알림을 드립니다.")
        } else {
            JobState.log("자동 일시정지: ${r.optString("message")}. 대기 중인 다음 작업도 멈춥니다. 시간이 지난 뒤 다시 시작하세요.")
        }
    }

    private fun export(gameId: Long, location: String, language: String) {
        JobState.log("출력 파일 생성 시작")
        updateNotification("출력 파일 생성 중", 0, 0)
        if (location.startsWith(APK_PREFIX)) {
            exportToApp(gameId, location.removePrefix(APK_PREFIX), language)
            return
        }
        val folder = GameFolder(contentResolver, Uri.parse(location))
        val font = prepareFont(gameId)
        var fontName: String? = null
        if (font != null) {
            try {
                folder.writeFontFile(language, font.name, font)
                fontName = font.name
                JobState.log("폰트 복사: tl/$language/rpytl/${font.name}")
            } catch (e: Exception) {
                JobState.log("폰트 복사 실패: ${e.message}")
            }
        }
        val existingTl = java.io.File(engine.workDir(gameId), "tl/$language").absolutePath
        val r = engine.export(gameId, fontName?.let { "tl/$language/rpytl/$it" }, prefs.forceLanguage, existingTl, prefs.fontMode)
        val info = r.getJSONObject("info")
        val n = folder.writeOutput(engine.outDir(gameId), r, fontName) { JobState.log(it) }
        JobState.log(
            "출력 완료: 파일 $n 개 → game/tl/$language/rpytl/ (대사 파일 ${info.getInt("dialogue_files")}개, " +
                "문자열 ${info.getInt("strings")}개, 기존 번역과 겹쳐 제외 ${info.getInt("skipped_existing")}개)"
        )
        logUnsafe(info)
    }

    /**
     * 게임에 넣을 폰트 파일을 캐시 폴더에 준비한다.
     * 사용자가 고른 폰트가 있으면 그것, 없으면 앱에 포함된 나눔고딕 (SIL OFL).
     */
    private fun prepareFont(gameId: Long): java.io.File? {
        val dir = java.io.File(cacheDir, "font").apply { mkdirs() }
        dir.listFiles()?.forEach { it.delete() }
        val uri = prefs.fontUri(gameId)
        return try {
            if (uri != null) {
                val out = java.io.File(dir, "font.${fontExt(uri)}")
                contentResolver.openInputStream(Uri.parse(uri))!!.use { i -> out.outputStream().use { i.copyTo(it) } }
                out
            } else {
                val out = java.io.File(dir, "font.ttf")
                assets.open("fonts/NanumGothic-Regular.ttf").use { i -> out.outputStream().use { i.copyTo(it) } }
                JobState.log("폰트: 앱에 포함된 나눔고딕 사용 (한글만 이 폰트로 표시, 영문은 게임 폰트 그대로)")
                out
            }
        } catch (e: Exception) {
            JobState.log("폰트 준비 실패: ${e.message}")
            null
        }
    }

    private fun logUnsafe(info: org.json.JSONObject) {
        val n = info.optInt("unsafe_moved_to_review")
        if (n > 0) {
            JobState.log(
                "주의: 태그/변수 구조가 원문과 달라 게임을 멈출 수 있는 번역 ${n}줄을 출력에서 뺐습니다 " +
                    "(검수필요로 이동, 게임에서는 원문 표시). '검수필요 보기'로 확인하세요."
            )
        }
    }

    private fun fontExt(fontUri: String): String {
        val display = runCatching {
            contentResolver.query(Uri.parse(fontUri), arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)
                ?.use { c -> if (c.moveToFirst()) c.getString(0) else null }
        }.getOrNull() ?: ""
        return if (display.lowercase().endsWith(".otf")) "otf" else "ttf"
    }

    private fun exportToApp(gameId: Long, pkg: String, language: String) {
        val st = ShizukuFs.status()
        if (st != ShizukuFs.Status.READY) {
            JobState.log("출력 실패: ${st.label}. Shizuku 를 켜고 앱에서 권한을 허용한 뒤 다시 시도하세요.")
            return
        }
        val font = prepareFont(gameId)
        val fontName = font?.name
        val existingTl = java.io.File(engine.workDir(gameId), "tl/$language").absolutePath
        val r = engine.export(gameId, fontName?.let { "tl/$language/rpytl/$it" }, prefs.forceLanguage, existingTl, prefs.fontMode)
        val info = r.getJSONObject("info")
        val n = AppGame(this, pkg).writeOutput(engine.outDir(gameId), r, font, fontName) {
            JobState.log(it)
        }
        JobState.log(
            "출력 완료: 파일 $n 개 → Android/data/$pkg/files/game/tl/$language/rpytl/ " +
                "(대사 파일 ${info.getInt("dialogue_files")}개, 문자열 ${info.getInt("strings")}개, " +
                "기존 번역과 겹쳐 제외 ${info.getInt("skipped_existing")}개). 게임을 완전히 종료 후 다시 실행하세요."
        )
        logUnsafe(info)
    }

    private fun updateNotification(text: String, done: Int, total: Int) {
        if (JobState.running.value != null) runCatching { acquireWakeLock() }   // 장시간 작업: 잠금 연장
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
