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

    /**
     * Google 속도 기본값 = 요청 제한(429)이 가장 덜 걸렸던 v0.8.0 시절 값 (간격 0.5초, 동시 3개, 요청당 25줄).
     * 이보다 빠르게 올리면 429 가 걸릴 수 있다 (설정 화면에 경고 표시).
     */
    companion object {
        const val DEFAULT_GOOGLE_INTERVAL_MS = 500
        const val DEFAULT_GOOGLE_WORKERS = 3
        const val DEFAULT_GOOGLE_BATCH = 25
    }

    /** 요청 시작 사이 최소 간격 (모든 동시 요청 공통). 예전 키(google_delay_ms)와 의미가 달라 키를 바꿈 */
    var googleDelayMs: Int
        get() = sp.getInt("google_interval_ms", DEFAULT_GOOGLE_INTERVAL_MS)
        set(v) = sp.edit().putInt("google_interval_ms", v).apply()

    /** 동시에 보낼 요청 수 (제한이 걸리면 자동으로 줄었다가 안정되면 복구) */
    var googleWorkers: Int
        get() = sp.getInt("google_workers", DEFAULT_GOOGLE_WORKERS)
        set(v) = sp.edit().putInt("google_workers", v).apply()

    /** 한 요청에 담는 줄 수 */
    var googleBatch: Int
        get() = sp.getInt("google_batch", DEFAULT_GOOGLE_BATCH)
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

    /** 게임에 이미 들어 있는 다른 번역(tl/<언어>)이 덮는 줄은 번역하지 않기 */
    var skipExisting: Boolean
        get() = sp.getBoolean("skip_existing", true)
        set(v) = sp.edit().putBoolean("skip_existing", v).apply()

    /** 번역할 때 캐릭터 분석 결과(성격/말투), 화자, 앞 대사를 로컬 모델 프롬프트에 넣기 */
    var useProfiles: Boolean
        get() = sp.getBoolean("use_profiles", true)
        set(v) = sp.edit().putBoolean("use_profiles", v).apply()

    /** 게임 코드(파이썬) 속 문자열도 번역 대상에 넣기: 아이템/스킬 설명, 상태창 글자 등 */
    var pyStrings: Boolean
        get() = sp.getBoolean("py_strings", true)
        set(v) = sp.edit().putBoolean("py_strings", v).apply()

    /** fallback = 한글만 한글 폰트로 그리고 영문은 게임 폰트 유지 (기본), replace = 주요 스타일 폰트를 통째로 교체 */
    var fontMode: String
        get() = sp.getString("font_mode", "fallback")!!
        set(v) = sp.edit().putString("font_mode", v).apply()

    // ---- Gemini API (엔진 "gemini"). 키는 이 앱 전용 저장소에만 저장되고 로그/URL 에 남지 않는다.
    var geminiKey: String
        get() = sp.getString("gemini_key", "")!!
        set(v) = sp.edit().putString("gemini_key", v.trim()).apply()

    /** 모델 이름. gemini-flash-latest 는 Flash 계열 최신을 가리키는 별칭 */
    var geminiModel: String
        get() = sp.getString("gemini_model", "gemini-flash-latest")!!
        set(v) = sp.edit().putString("gemini_model", v.trim()).apply()

    /** 분당 요청 수 상한 (무료 등급은 낮다. 429 가 나면 앱이 알아서 더 늦춘다) */
    var geminiRpm: Int
        get() = sp.getInt("gemini_rpm", 10)
        set(v) = sp.edit().putInt("gemini_rpm", v).apply()

    /** 한 요청에 담는 줄 수 (크게 하면 하루 요청 한도를 아끼지만 번호 목록이 어긋날 수 있다) */
    var geminiBatch: Int
        get() = sp.getInt("gemini_batch", 20)
        set(v) = sp.edit().putInt("gemini_batch", v).apply()

    fun geminiOpts(): JSONObject = JSONObject()
        .put("api_key", geminiKey).put("model", geminiModel).put("rpm", geminiRpm)

    /** 속도 설정을 기본값(안전한 값)으로 */
    fun resetGoogleSpeed() {
        sp.edit().remove("google_interval_ms").remove("google_workers").remove("google_batch").apply()
    }

    fun fontUri(gameId: Long): String? = sp.getString("font_$gameId", null)
    fun setFontUri(gameId: Long, uri: String?) = sp.edit().putString("font_$gameId", uri).apply()

    fun translateOpts(engine: String, retryReview: Boolean): JSONObject = JSONObject().apply {
        put("source_lang", sourceLang)
        if (targetCode.isNotBlank()) put("target_code", targetCode.trim())
        put("retry_review", retryReview)
        put("use_profiles", useProfiles)
        if (engine == "google" && fallbackLocal) put("fallback", "local")
        put(
            "google",
            JSONObject().put("delay", googleDelayMs / 1000.0).put("max_batch", googleBatch)
                .put("workers", googleWorkers),
        )
        put("local", JSONObject().put("max_batch", localBatch).put("cooldown", localCooldownMs / 1000.0))
        if (engine == "gemini") put("gemini", geminiOpts().put("max_batch", geminiBatch))
    }
}
