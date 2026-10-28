package com.wlee41044.rpytl.llm

import android.content.Context
import com.wlee41044.rpytl.Prefs

/**
 * 로컬 LLM(llama.cpp GGUF) 브리지. Python 의 translators.local.LocalTranslator 가
 * generate(prompt, maxTokens) 를 호출한다. 4단계에서 JNI 로 구현.
 */
interface LocalLlm : AutoCloseable {
    fun generate(prompt: String, maxTokens: Int): String

    companion object {
        /** 모델을 로드해서 돌려준다. 아직 미구현이면 null (Python 쪽에서 명확한 오류로 처리). */
        fun create(context: Context, prefs: Prefs, log: (String) -> Unit): LocalLlm? {
            log("로컬 모델 연동은 아직 구현되지 않았습니다 (4단계)")
            return null
        }
    }
}
