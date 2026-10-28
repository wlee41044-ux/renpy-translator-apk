package com.wlee41044.rpytl.llm

/** llama.cpp (JNI, libs/rpytl_llama.so) */
class LlamaLlm private constructor(
    private var handle: Long,
    private val noThink: Boolean,
    private val temperature: Float,
) : LocalLlm {

    private val template: String = String(nativeTemplate(handle), Charsets.UTF_8)

    /**
     * Qwen3 등 생각(thinking) 모드가 있는 ChatML 모델은 빈 think 블록을 미리 넣어 생각을 끈다.
     * (Qwen3 의 enable_thinking=False 와 같은 효과)
     */
    private val assistantPrefix: String =
        if (noThink && template.contains("<|im_start|>") && template.contains("think")) "<think>\n\n</think>\n\n" else ""

    @Synchronized
    override fun chat(system: String, user: String, maxTokens: Int): String {
        check(handle != 0L) { "model closed" }
        val out = nativeChat(
            handle, system.toByteArray(), user.toByteArray(), assistantPrefix.toByteArray(),
            maxTokens, temperature,
        )
        return String(out, Charsets.UTF_8)
    }

    override fun cancel() {
        val h = handle
        if (h != 0L) nativeCancel(h)
    }

    @Synchronized
    override fun close() {
        LocalLlm.clearActive(this)
        if (handle != 0L) {
            nativeFree(handle)
            handle = 0
        }
    }

    companion object {
        init {
            System.loadLibrary("rpytl_llama")
        }

        fun load(path: String, nCtx: Int, threads: Int, noThink: Boolean, temperature: Float): LlamaLlm =
            LlamaLlm(nativeLoad(path.toByteArray(), nCtx, threads), noThink, temperature)

        @JvmStatic private external fun nativeLoad(path: ByteArray, nCtx: Int, nThreads: Int): Long
        @JvmStatic private external fun nativeTemplate(handle: Long): ByteArray
        @JvmStatic private external fun nativeChat(
            handle: Long, system: ByteArray, user: ByteArray, assistantPrefix: ByteArray, maxTokens: Int, temperature: Float,
        ): ByteArray
        @JvmStatic private external fun nativeCancel(handle: Long)
        @JvmStatic private external fun nativeFree(handle: Long)
    }
}
