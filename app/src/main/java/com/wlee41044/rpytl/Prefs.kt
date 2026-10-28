package com.wlee41044.rpytl

import android.content.Context
import org.json.JSONObject

/** 설정 (SharedPreferences). 개인용이라 화면에서 바로 고친다. */
class Prefs(context: Context) {
    private val sp = context.applicationContext.getSharedPreferences("settings", Context.MODE_PRIVATE)

    var engine: String
        get() = sp.getString("engine", "google")!!
        set(v) = sp.edit().putString("engine", v).apply()

    /** 원문 언어 (Google 코드). auto = 자동 감지 */
    var sourceLang: String
        get() = sp.getString("source_lang", "auto")!!
        set(v) = sp.edit().putString("source_lang", v).apply()

    /** 번역 대상 Google 언어 코드. 비우면 게임 언어(korean→ko) 로 자동 */
    var targetCode: String
        get() = sp.getString("target_code", "")!!
        set(v) = sp.edit().putString("target_code", v).apply()

    /** 요청 시작 사이 최소 간격 (모든 동시 요청 공통). 예전 키(google_delay_ms)와 의미가 달라 키를 바꿈 */
    var googleDelayMs: Int
        get() = sp.getInt("google_interval_ms", 500)
        set(v) = sp.edit().putInt("google_interval_ms", v).apply()

    /** 동시에 보낼 요청 수 (제한이 걸리면 자동으로 줄었다가 안정되면 복구) */
    var googleWorkers: Int
        get() = sp.getInt("google_workers", 3)
        set(v) = sp.edit().putInt("google_workers", v).apply()

    var googleBatch: Int
        get() = sp.getInt("google_batch", 25)
        set(v) = sp.edit().putInt("google_batch", v).apply()

    /** Google 실패 시 로컬 모델로 자동 전환 */
    var fallbackLocal: Boolean
        get() = sp.getBoolean("fallback_local", false)
        set(v) = sp.edit().putBoolean("fallback_local", v).apply()

    var localBatch: Int
        get() = sp.getInt("local_batch", 4)
        set(v) = sp.edit().putInt("local_batch", v).apply()

    var localThreads: Int
        get() = sp.getInt("local_threads", 4)
        set(v) = sp.edit().putInt("local_threads", v).apply()

    /** 컨텍스트 길이. 길면 메모리를 많이 쓴다 (4B Q4 + 4096 ≈ 3GB) */
    var localCtx: Int
        get() = sp.getInt("local_ctx", 4096)
        set(v) = sp.edit().putInt("local_ctx", v).apply()

    /** Qwen3 등의 생각(thinking) 모드 끄기 */
    var localNoThink: Boolean
        get() = sp.getBoolean("local_no_think", true)
        set(v) = sp.edit().putBoolean("local_no_think", v).apply()

    /** 0 = 그리디 (일관성 우선) */
    var localTemperature: Float
        get() = sp.getFloat("local_temp", 0f)
        set(v) = sp.edit().putFloat("local_temp", v).apply()

    /** 배치 사이 쉬는 시간 (발열 조절) */
    var localCooldownMs: Int
        get() = sp.getInt("local_cooldown_ms", 0)
        set(v) = sp.edit().putInt("local_cooldown_ms", v).apply()

    var modelPath: String
        get() = sp.getString("model_path", "")!!
        set(v) = sp.edit().putString("model_path", v).apply()

    /** 생성한 언어 초기화 스크립트에서 config.language 로 언어를 강제할지 */
    var forceLanguage: Boolean
        get() = sp.getBoolean("force_language", true)
        set(v) = sp.edit().putBoolean("force_language", v).apply()

    /** fallback = 한글만 한글 폰트로 그리고 영문은 게임 폰트 유지 (기본), replace = 주요 스타일 폰트를 통째로 교체 */
    var fontMode: String
        get() = sp.getString("font_mode", "fallback")!!
        set(v) = sp.edit().putString("font_mode", v).apply()

    fun fontUri(gameId: Long): String? = sp.getString("font_$gameId", null)
    fun setFontUri(gameId: Long, uri: String?) = sp.edit().putString("font_$gameId", uri).apply()

    fun translateOpts(engine: String, retryReview: Boolean): JSONObject = JSONObject().apply {
        put("source_lang", sourceLang)
        if (targetCode.isNotBlank()) put("target_code", targetCode.trim())
        put("retry_review", retryReview)
        if (engine == "google" && fallbackLocal) put("fallback", "local")
        put(
            "google",
            JSONObject().put("delay", googleDelayMs / 1000.0).put("max_batch", googleBatch)
                .put("workers", googleWorkers),
        )
        put("local", JSONObject().put("max_batch", localBatch).put("cooldown", localCooldownMs / 1000.0))
    }
}
