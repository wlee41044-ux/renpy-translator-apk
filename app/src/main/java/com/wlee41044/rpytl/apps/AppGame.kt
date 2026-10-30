package com.wlee41044.rpytl.apps

import android.content.Context
import android.net.Uri
import android.os.ParcelFileDescriptor
import com.wlee41044.rpytl.Engine
import com.wlee41044.rpytl.PyControl
import com.wlee41044.rpytl.shizuku.ShizukuFs
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * 설치된 Ren'Py 앱 게임.
 * 읽기: APK(assets/x-game/) 를 직접 읽는다 (권한 불필요).
 * 쓰기: Ren'Py 안드로이드는 Android/data/<패키지>/files/game/ 을 검색 경로 맨 앞에 넣으므로
 *       (renpy.py predefined_searchpath, 7.4 이상) 그 아래 tl/<언어>/rpytl/ 에 Shizuku 로 쓴다.
 */
class AppGame(private val context: Context, val pkg: String) {

    fun importScripts(engine: Engine, gameId: Long, language: String, control: PyControl): Int {
        val ai = RenpyApps.info(context, pkg)
        val apks = RenpyApps.apkPaths(ai)
        engine.resetWorkdir(gameId)
        val r = engine.extractApks(apks, gameId, language, control)
        var n = r.getInt("archives") + r.getInt("files")
        control.log("APK 에서 스크립트 ${r.getJSONArray("names").length()}개 추출 (아카이브 ${r.getInt("archives")}개)")
        n += importExternal(engine.workDir(gameId), language, control)
        return n
    }

    /** 이미 Android/data/.../game 에 있는 패치 스크립트 (있으면 APK 보다 우선) */
    private fun importExternal(work: File, language: String, control: PyControl): Int {
        if (ShizukuFs.status() != ShizukuFs.Status.READY) {
            control.log("Shizuku 미연결: Android/data 쪽 기존 패치 파일은 건너뜀 (보통 없음)")
            return 0
        }
        val fs = ShizukuFs.get()
        val dir = ShizukuFs.gameDir(pkg)
        val arr = JSONArray(fs.list(dir))
        var n = 0
        for (i in 0 until arr.length()) {
            val rel = arr.getJSONObject(i).getString("p")
            val low = rel.lowercase()
            if (!(low.endsWith(".rpy") || low.endsWith(".rpyc") || low.endsWith(".rpym") || low.endsWith(".rpymc"))) continue
            val parts = low.split("/")
            if (parts[0] == "tl" && (parts.size < 3 || parts[1] != language.lowercase() || parts[2] == "rpytl")) continue
            if (parts[0] == "saves" || parts[0] == "cache") continue
            val dest = File(work, rel)
            dest.parentFile?.mkdirs()
            fs.openRead("$dir/$rel").use { pfd ->
                ParcelFileDescriptor.AutoCloseInputStream(pfd).use { input -> dest.outputStream().use { input.copyTo(it) } }
            }
            n++
        }
        if (n > 0) control.log("Android/data 의 패치 스크립트 ${n}개 반영")
        return n
    }

    /** export 결과를 Android/data/<pkg>/files/game/tl/<lang>/rpytl/ 로. 오래된 파일(.rpyc 포함)은 지운다. */
    fun writeOutput(outDir: File, result: JSONObject, fontFile: File?, fontName: String?, log: (String) -> Unit): Int {
        val fs = ShizukuFs.get()
        val files = result.getJSONArray("files")
        val rels = (0 until files.length()).map { files.getString(it) }
        if (rels.isEmpty()) return 0
        check(fs.exists(ShizukuFs.filesDir(pkg))) {
            "Android/data/$pkg/files 가 없습니다. 게임을 한 번 실행한 뒤 다시 시도하세요."
        }
        val game = ShizukuFs.gameDir(pkg)
        val baseRel = rels.first().split("/").take(3).joinToString("/") // tl/<lang>/rpytl
        val base = "$game/$baseRel"
        fs.mkdirs(base)

        val wanted = rels.map { it.removePrefix("$baseRel/") }.toSet() + listOfNotNull(fontName)
        val existing = JSONArray(fs.list(base))
        for (i in 0 until existing.length()) {
            val rel = existing.getJSONObject(i).getString("p")
            if (rel !in wanted && fs.remove("$base/$rel")) log("삭제: $rel")
        }
        for (rel in rels) {
            ParcelFileDescriptor.open(File(outDir, rel), ParcelFileDescriptor.MODE_READ_ONLY).use { pfd ->
                fs.write("$game/$rel", pfd)
            }
        }
        if (fontFile != null && fontName != null) {
            ParcelFileDescriptor.open(fontFile, ParcelFileDescriptor.MODE_READ_ONLY).use { pfd -> fs.write("$base/$fontName", pfd) }
            log("폰트 복사: $baseRel/$fontName")
        }
        return rels.size
    }

    /** 이 앱이 넣은 번역(game/tl/<lang>/rpytl/)을 전부 지운다. 게임 원본 파일은 건드리지 않는다. 반환: 지운 파일 수 */
    fun removeOutput(language: String, log: (String) -> Unit): Int {
        val fs = ShizukuFs.get()
        val base = "${ShizukuFs.gameDir(pkg)}/tl/$language/rpytl"
        if (!fs.exists(base)) return 0
        val existing = JSONArray(fs.list(base))
        var n = 0
        for (i in 0 until existing.length()) {
            val rel = existing.getJSONObject(i).getString("p")
            if (fs.remove("$base/$rel")) n++ else log("삭제 실패: $rel")
        }
        fs.remove(base)   // 비었으면 폴더도 지운다 (안 비었으면 그냥 남는다)
        return n
    }
}
