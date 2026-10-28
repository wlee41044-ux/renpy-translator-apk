package com.wlee41044.rpytl.saf

import android.content.ContentResolver
import android.net.Uri
import com.wlee41044.rpytl.Engine
import org.json.JSONObject
import java.io.File

/**
 * 사용자가 고른 폴더 = 게임 루트(game/ 를 포함) 또는 game/ 폴더 자체.
 * 원본은 읽기만 하고, 쓰기는 game/tl/<언어>/rpytl/ 아래에만 한다.
 */
class GameFolder(resolver: ContentResolver, treeUri: Uri) {
    val saf = Saf(resolver, treeUri)

    /** game/ 폴더 문서 id 와 (알 수 있으면) 루트 문서 id */
    val gameId: String
    val rootId: String?

    init {
        val game = saf.child(saf.rootId, "game")
        if (game != null && game.isDir) {
            gameId = game.id
            rootId = saf.rootId
        } else {
            gameId = saf.rootId
            rootId = null
        }
    }

    fun looksLikeRenpy(): Boolean = saf.list(gameId).any {
        val n = it.name.lowercase()
        !it.isDir && (n.endsWith(".rpa") || n.endsWith(".rpy") || n.endsWith(".rpyc"))
    }

    /** 루트의 renpy/vc_version.py (없으면 renpy/__init__.py) 내용: 식별자 규칙 판단용 */
    fun renpyVersionText(): String? {
        val root = rootId ?: return null
        val renpy = saf.child(root, "renpy")?.takeIf { it.isDir } ?: return null
        val f = saf.child(renpy.id, "vc_version.py") ?: saf.child(renpy.id, "__init__.py") ?: return null
        return runCatching { saf.readText(f) }.getOrNull()
    }

    /**
     * 스크립트를 앱 작업 폴더로 가져온다: .rpa 는 스크립트 멤버만 추출, 그다음 느슨한 파일을 덮어쓴다
     * (Ren'Py 도 아카이브보다 폴더의 파일을 우선한다).
     */
    fun importScripts(engine: Engine, appGameId: Long, language: String, log: (String) -> Unit): Int {
        engine.resetWorkdir(appGameId)
        val work = engine.workDir(appGameId)
        val archives = ArrayList<Saf.Doc>()
        val loose = ArrayList<Pair<Saf.Doc, String>>()
        saf.walk(gameId, "", onDir = { d, rel ->
            val low = rel.lowercase()
            when {
                low == "saves" || low == "cache" || low.startsWith("saves/") -> false
                low == "tl" -> true
                low.startsWith("tl/") -> {
                    // 기존 번역은 같은 언어만 (중복 방지용으로 읽음). 우리 rpytl 폴더는 제외
                    val parts = low.split("/")
                    parts[1] == language.lowercase() && (parts.size < 3 || parts[2] != "rpytl")
                }
                else -> true
            }
        }, onFile = { d, rel ->
            val low = d.name.lowercase()
            when {
                // Ren'Py 는 game/ 바로 아래의 아카이브만 읽는다
                low.endsWith(".rpa") -> if (!rel.contains('/')) archives.add(d)
                low.endsWith(".rpy") || low.endsWith(".rpyc") || low.endsWith(".rpym") || low.endsWith(".rpymc") ->
                    loose.add(d to rel)
            }
        })
        for (a in archives.sortedBy { it.name }) {
            log("아카이브 추출: ${a.name} (${a.size / 1024 / 1024}MB)")
            val pfd = saf.resolver.openFileDescriptor(a.uri(saf.treeUri), "r")
                ?: throw IllegalStateException("열 수 없음: ${a.name}")
            pfd.use {
                val names = engine.extractArchiveFd(it.fd, appGameId)
                log("  스크립트 ${names.length()}개")
            }
        }
        for ((d, rel) in loose) {
            saf.copyToFile(d, File(work, rel))
        }
        log("폴더의 스크립트 ${loose.size}개 복사")
        return archives.size + loose.size
    }

    /**
     * export 결과(out/tl/<lang>/rpytl/...)를 game/tl/<lang>/rpytl/ 로 기록.
     * 새 목록에 없는 기존 파일(.rpyc 포함)은 지운다: 남은 .rpyc 가 중복 번역 오류를 낼 수 있음.
     */
    fun writeOutput(outDir: File, result: JSONObject, fontName: String?, log: (String) -> Unit): Int {
        val files = result.getJSONArray("files")
        val rels = (0 until files.length()).map { files.getString(it) }
        if (rels.isEmpty()) return 0
        // 모든 경로는 tl/<lang>/rpytl/... 형태
        val base = rels.first().split("/").take(3)
        val baseId = saf.mkdirs(gameId, base)

        val wanted = rels.map { it.split("/").drop(3).joinToString("/") }.toSet()
        val keep = wanted + listOfNotNull(fontName)
        // 오래된 파일 정리
        saf.walk(baseId, "", onDir = { _, _ -> true }, onFile = { d, rel ->
            if (rel !in keep) {
                if (saf.delete(d)) log("삭제: $rel")
            }
        })
        for (rel in rels) {
            val parts = rel.split("/")
            val dirId = if (parts.size > 4) saf.mkdirs(baseId, parts.subList(3, parts.size - 1)) else baseId
            saf.writeFile(dirId, parts.last(), File(outDir, rel))
        }
        return rels.size
    }

    fun writeFontFile(language: String, name: String, source: File) {
        val baseId = saf.mkdirs(gameId, listOf("tl", language, "rpytl"))
        saf.writeFile(baseId, name, source)
    }

    fun writeFont(language: String, name: String, source: Uri) {
        val baseId = saf.mkdirs(gameId, listOf("tl", language, "rpytl"))
        saf.writeFromUri(baseId, name, source)
    }
}
