// llama.cpp JNI 브리지: 모델 로드, 채팅 템플릿 적용, 그리디/샘플링 생성.
// Kotlin: com.wlee41044.rpytl.llm.LlamaLlm

#include <android/log.h>
#include <jni.h>

#include <algorithm>
#include <atomic>
#include <mutex>
#include <string>
#include <vector>

#include "llama.h"

#define TAG "rpytl-llama"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, TAG, __VA_ARGS__)

namespace {

struct Handle {
    llama_model *model = nullptr;
    llama_context *ctx = nullptr;
    const llama_vocab *vocab = nullptr;
    std::string tmpl;
    std::atomic<bool> cancel{false};
};

std::once_flag g_init;

void log_cb(ggml_log_level level, const char *text, void *) {
    if (level >= GGML_LOG_LEVEL_WARN) __android_log_print(ANDROID_LOG_WARN, TAG, "%s", text);
}

void throw_ise(JNIEnv *env, const std::string &msg) {
    jclass cls = env->FindClass("java/lang/IllegalStateException");
    if (cls) env->ThrowNew(cls, msg.c_str());
}

std::string jstr(JNIEnv *env, jbyteArray a) {
    // 문자열은 UTF-8 바이트 배열로 주고받는다 (NewStringUTF 는 modified UTF-8 이라 이모지 등이 깨짐)
    if (!a) return {};
    jsize n = env->GetArrayLength(a);
    std::string s(static_cast<size_t>(n), '\0');
    env->GetByteArrayRegion(a, 0, n, reinterpret_cast<jbyte *>(s.data()));
    return s;
}

jbyteArray jbytes(JNIEnv *env, const std::string &s) {
    jbyteArray a = env->NewByteArray(static_cast<jsize>(s.size()));
    env->SetByteArrayRegion(a, 0, static_cast<jsize>(s.size()), reinterpret_cast<const jbyte *>(s.data()));
    return a;
}

}  // namespace

extern "C" JNIEXPORT jlong JNICALL
Java_com_wlee41044_rpytl_llm_LlamaLlm_nativeLoad(JNIEnv *env, jclass, jbyteArray jpath, jint n_ctx, jint n_threads) {
    std::call_once(g_init, [] {
        llama_log_set(log_cb, nullptr);
        llama_backend_init();
    });
    std::string path = jstr(env, jpath);

    llama_model_params mp = llama_model_default_params();
    mp.n_gpu_layers = 0;
    llama_model *model = llama_model_load_from_file(path.c_str(), mp);
    if (!model) {
        throw_ise(env, "모델 로드 실패: " + path);
        return 0;
    }

    llama_context_params cp = llama_context_default_params();
    cp.n_ctx = static_cast<uint32_t>(n_ctx);
    cp.n_batch = 512;
    cp.n_ubatch = 512;
    cp.n_threads = n_threads;
    cp.n_threads_batch = n_threads;
    cp.no_perf = true;
    llama_context *ctx = llama_init_from_model(model, cp);
    if (!ctx) {
        llama_model_free(model);
        throw_ise(env, "컨텍스트 생성 실패 (n_ctx 를 줄여 보세요)");
        return 0;
    }

    auto *h = new Handle();
    h->model = model;
    h->ctx = ctx;
    h->vocab = llama_model_get_vocab(model);
    const char *t = llama_model_chat_template(model, nullptr);
    h->tmpl = t ? t : "chatml";
    LOGI("model loaded: ctx=%d threads=%d", n_ctx, n_threads);
    return reinterpret_cast<jlong>(h);
}

extern "C" JNIEXPORT jbyteArray JNICALL
Java_com_wlee41044_rpytl_llm_LlamaLlm_nativeTemplate(JNIEnv *env, jclass, jlong handle) {
    auto *h = reinterpret_cast<Handle *>(handle);
    return jbytes(env, h ? h->tmpl : std::string());
}

extern "C" JNIEXPORT jbyteArray JNICALL
Java_com_wlee41044_rpytl_llm_LlamaLlm_nativeChat(JNIEnv *env, jclass, jlong handle, jbyteArray jsystem,
                                                 jbyteArray juser, jbyteArray jassistant_prefix, jint max_tokens,
                                                 jfloat temperature) {
    auto *h = reinterpret_cast<Handle *>(handle);
    if (!h) {
        throw_ise(env, "model not loaded");
        return nullptr;
    }
    h->cancel = false;
    std::string system = jstr(env, jsystem);
    std::string user = jstr(env, juser);
    std::string prefix = jstr(env, jassistant_prefix);

    llama_chat_message msgs[2] = {{"system", system.c_str()}, {"user", user.c_str()}};
    std::vector<char> buf(system.size() * 2 + user.size() * 2 + 1024);
    std::string tmpl = h->tmpl;
    int32_t n = llama_chat_apply_template(tmpl.c_str(), msgs, 2, true, buf.data(), (int32_t) buf.size());
    if (n < 0) {
        // 모델 템플릿을 llama.cpp 가 모르면 chatml 로
        tmpl = "chatml";
        n = llama_chat_apply_template(tmpl.c_str(), msgs, 2, true, buf.data(), (int32_t) buf.size());
    }
    if (n > (int32_t) buf.size()) {
        buf.resize(n + 1);
        n = llama_chat_apply_template(tmpl.c_str(), msgs, 2, true, buf.data(), (int32_t) buf.size());
    }
    if (n < 0) {
        throw_ise(env, "채팅 템플릿 적용 실패");
        return nullptr;
    }
    std::string prompt(buf.data(), n);
    prompt += prefix;

    // 토큰화
    const bool add_bos = llama_vocab_get_add_bos(h->vocab);
    int32_t nt = -llama_tokenize(h->vocab, prompt.c_str(), (int32_t) prompt.size(), nullptr, 0, add_bos, true);
    std::vector<llama_token> tokens(nt > 0 ? nt : 1);
    nt = llama_tokenize(h->vocab, prompt.c_str(), (int32_t) prompt.size(), tokens.data(), (int32_t) tokens.size(),
                        add_bos, true);
    if (nt < 0) {
        throw_ise(env, "토큰화 실패");
        return nullptr;
    }
    tokens.resize(nt);
    const int n_ctx = (int) llama_n_ctx(h->ctx);
    if (nt + max_tokens > n_ctx) {
        throw_ise(env, "프롬프트가 너무 깁니다 (" + std::to_string(nt) + "+" + std::to_string(max_tokens) +
                           " > ctx " + std::to_string(n_ctx) + "). 배치 크기를 줄이세요.");
        return nullptr;
    }

    llama_memory_clear(llama_get_memory(h->ctx), true);

    const int n_batch = (int) llama_n_batch(h->ctx);
    for (int i = 0; i < nt; i += n_batch) {
        int cnt = std::min(n_batch, nt - i);
        if (llama_decode(h->ctx, llama_batch_get_one(tokens.data() + i, cnt)) != 0) {
            throw_ise(env, "프롬프트 처리 실패 (decode)");
            return nullptr;
        }
        if (h->cancel) return jbytes(env, "");
    }

    llama_sampler_chain_params sp = llama_sampler_chain_default_params();
    sp.no_perf = true;
    llama_sampler *smpl = llama_sampler_chain_init(sp);
    if (temperature <= 0.0f) {
        llama_sampler_chain_add(smpl, llama_sampler_init_greedy());
    } else {
        llama_sampler_chain_add(smpl, llama_sampler_init_top_k(40));
        llama_sampler_chain_add(smpl, llama_sampler_init_top_p(0.9f, 1));
        llama_sampler_chain_add(smpl, llama_sampler_init_temp(temperature));
        llama_sampler_chain_add(smpl, llama_sampler_init_dist(1234));
    }

    std::string out;
    char piece[256];
    for (int i = 0; i < max_tokens && !h->cancel; i++) {
        llama_token tok = llama_sampler_sample(smpl, h->ctx, -1);
        if (llama_vocab_is_eog(h->vocab, tok)) break;
        int len = llama_token_to_piece(h->vocab, tok, piece, sizeof(piece), 0, false);
        if (len > 0) out.append(piece, len);
        if (llama_decode(h->ctx, llama_batch_get_one(&tok, 1)) != 0) {
            LOGE("decode failed at token %d", i);
            break;
        }
    }
    llama_sampler_free(smpl);
    return jbytes(env, out);
}

extern "C" JNIEXPORT void JNICALL
Java_com_wlee41044_rpytl_llm_LlamaLlm_nativeCancel(JNIEnv *, jclass, jlong handle) {
    auto *h = reinterpret_cast<Handle *>(handle);
    if (h) h->cancel = true;
}

extern "C" JNIEXPORT void JNICALL
Java_com_wlee41044_rpytl_llm_LlamaLlm_nativeFree(JNIEnv *, jclass, jlong handle) {
    auto *h = reinterpret_cast<Handle *>(handle);
    if (!h) return;
    if (h->ctx) llama_free(h->ctx);
    if (h->model) llama_model_free(h->model);
    delete h;
}
