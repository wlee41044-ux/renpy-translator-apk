package com.wlee41044.rpytl

import android.content.Context
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/** Python 엔진(rpytl.pipeline) 호출 래퍼. 모든 함수는 백그라운드 스레드에서 부를 것. */
class Engine(context: Context) {
    private val app = context.applicationContext
    val dbPath: String = File(app.filesDir, "rpytl.db").absolutePath

    private val py: PyObject by lazy { Python.getInstance().getModule("rpytl.pipeline") }

    fun workDir(gameId: Long) = File(app.filesDir, "work/$gameId/game")
    fun outDir(gameId: Long) = File(app.filesDir, "out/$gameId")

    fun addGame(name: String, folderUri: String, language: String): Long =
        py.callAttr("add_game", dbPath, name, folderUri, language).toLong()

    /** 번역 기록 DB 백업 (작업 시작 전). 실패해도 작업은 계속한다. */
    fun backup(): String = runCatching { py.callAttr("backup_db", dbPath).toString() }.getOrDefault("")

    fun listGames(): List<GameInfo> {
        val arr = JSONArray(py.callAttr("list_games", dbPath).toString())
        return (0 until arr.length()).map { GameInfo.from(arr.getJSONObject(it)) }
    }

    fun stats(gameId: Long): Stats = Stats.from(JSONObject(py.callAttr("stats", dbPath, gameId).toString()))

    fun setLanguage(gameId: Long, language: String) {
        py.callAttr("set_language", dbPath, gameId, language)
    }

    fun deleteGame(gameId: Long) {
        py.callAttr("delete_game", dbPath, gameId)
        File(app.filesDir, "work/$gameId").deleteRecursively()
        outDir(gameId).deleteRecursively()
    }

    fun resetWorkdir(gameId: Long) {
        py.callAttr("reset_workdir", workDir(gameId).absolutePath)
    }

    fun extractArchiveFd(fd: Int, gameId: Long): JSONArray =
        JSONArray(py.callAttr("extract_archive_fd", fd, workDir(gameId).absolutePath).toString())

    fun extractApks(apks: List<String>, gameId: Long, language: String, control: PyControl): JSONObject =
        JSONObject(
            py.callAttr("extract_apks", JSONArray(apks).toString(), workDir(gameId).absolutePath, language, control)
                .toString()
        )

    fun scan(gameId: Long, renpyVersionText: String?, pyStrings: Boolean = true, skipExisting: Boolean = true): JSONObject =
        JSONObject(
            py.callAttr("scan_game", dbPath, gameId, workDir(gameId).absolutePath, renpyVersionText, pyStrings, skipExisting)
                .toString()
        )

    fun export(
        gameId: Long, font: String?, forceLanguage: Boolean, existingTlDir: String?, fontMode: String,
    ): JSONObject =
        JSONObject(
            py.callAttr(
                "export", dbPath, gameId, outDir(gameId).absolutePath, font, forceLanguage, existingTlDir, fontMode,
            ).toString()
        )

    fun translate(gameId: Long, engine: String, opts: JSONObject, control: PyControl, localBridge: Any?): JSONObject =
        JSONObject(py.callAttr("translate", dbPath, gameId, engine, opts.toString(), control, localBridge).toString())

    /** 엔진별 번역된 줄 수 {"google": n, "local": n} */
    fun engineCounts(gameId: Long): JSONObject = JSONObject(py.callAttr("engine_counts", dbPath, gameId).toString())

    /** 번역을 지우고 대기로 되돌린다 (지우기 전에 DB 백업). engine=null 이면 전부. 반환: 되돌린 줄 수 */
    fun resetTranslations(gameId: Long, engine: String?): Int =
        JSONObject(py.callAttr("reset_translations", dbPath, gameId, engine).toString()).getInt("reset")

    fun analyze(gameId: Long, opts: JSONObject, control: PyControl, localBridge: Any?): JSONObject =
        JSONObject(py.callAttr("analyze", dbPath, gameId, opts.toString(), control, localBridge).toString())

    /** 키 확인 겸 쓸 수 있는 Gemini 모델 이름 목록. 실패하면 예외 (메시지에 사유). */
    fun geminiModels(apiKey: String): List<String> {
        val arr = JSONArray(py.callAttr("gemini_models", apiKey).toString())
        return (0 until arr.length()).map { arr.getString(it) }
    }

    fun characters(gameId: Long): JSONArray = JSONArray(py.callAttr("characters", dbPath, gameId).toString())

    fun setProfile(gameId: Long, name: String, profile: String) {
        py.callAttr("set_profile", dbPath, gameId, name, profile)
    }

    fun reviewSummary(gameId: Long): JSONArray = JSONArray(py.callAttr("review_summary", dbPath, gameId).toString())

    fun reviewList(gameId: Long): JSONArray = JSONArray(py.callAttr("review_list", dbPath, gameId, 200).toString())
}

/** Python 쪽 runner 가 호출하는 콜백 (메서드 이름은 runner._Ctl 과 맞출 것). */
interface PyControl {
    fun isStopped(): Boolean
    fun log(msg: String)
    fun progress(done: Int, total: Int)
}

data class Stats(
    val total: Int, val done: Int, val pending: Int, val review: Int, val unused: Int, val progress: Double,
) {
    companion object {
        fun from(o: JSONObject) = Stats(
            o.optInt("total"), o.optInt("done"), o.optInt("pending"), o.optInt("review"),
            o.optInt("unused"), o.optDouble("progress", 0.0),
        )
        val EMPTY = Stats(0, 0, 0, 0, 0, 0.0)
    }
}

data class GameInfo(val id: Long, val name: String, val folderUri: String, val language: String, val stats: Stats) {
    companion object {
        fun from(o: JSONObject) = GameInfo(
            o.getLong("id"), o.getString("name"), o.getString("folder_uri"), o.getString("language"),
            o.optJSONObject("stats")?.let { Stats.from(it) } ?: Stats.EMPTY,
        )
    }
}
