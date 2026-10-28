package com.wlee41044.rpytl.llm

import android.content.Context
import com.wlee41044.rpytl.Prefs
import java.io.File

/**
 * 로컬 LLM 브리지. Python 의 translators.local.LocalTranslator 가 chat(system, user, maxTokens) 를 호출한다.
 */
interface LocalLlm : AutoCloseable {
    fun chat(system: String, user: String, maxTokens: Int): String

    /** 생성 중이면 중단 (일시정지) */
    fun cancel()

    companion object {
        @Volatile
        var active: LocalLlm? = null
            private set

        /** 설정의 모델을 로드. 실패하면 로그를 남기고 null (Python 쪽이 명확한 오류로 처리). */
        fun create(context: Context, prefs: Prefs, log: (String) -> Unit): LocalLlm? {
            val path = prefs.modelPath.trim()
            if (path.isEmpty()) {
                log("로컬 모델 파일이 지정되지 않았습니다 (설정 → 모델 파일 선택)")
                return null
            }
            if (!File(path).canRead()) {
                log("모델 파일을 읽을 수 없습니다: $path")
                return null
            }
            return try {
                val t0 = System.currentTimeMillis()
                log("로컬 모델 로드 중: ${File(path).name} (쓰레드 ${prefs.localThreads}, ctx ${prefs.localCtx})")
                val llm = LlamaLlm.load(path, prefs.localCtx, prefs.localThreads, prefs.localNoThink, prefs.localTemperature)
                log("로드 완료 (${(System.currentTimeMillis() - t0) / 1000}s)")
                active = llm
                llm
            } catch (t: Throwable) {
                log("로컬 모델 로드 실패: ${t.message}")
                null
            }
        }

        fun cancelActive() {
            active?.cancel()
        }

        internal fun clearActive(llm: LocalLlm) {
            if (active === llm) active = null
        }
    }
}
