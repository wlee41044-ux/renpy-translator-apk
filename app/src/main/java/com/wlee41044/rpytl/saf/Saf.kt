package com.wlee41044.rpytl.saf

import android.content.ContentResolver
import android.net.Uri
import android.provider.DocumentsContract
import android.provider.DocumentsContract.Document
import java.io.File
import java.io.FileNotFoundException

/**
 * SAF(Storage Access Framework) 트리 접근. DocumentFile 대신 DocumentsContract 를 직접 써서
 * 폴더당 쿼리 1번으로 목록을 얻는다 (게임 폴더는 파일이 많아 DocumentFile 이 매우 느림).
 */
class Saf(val resolver: ContentResolver, val treeUri: Uri) {

    data class Doc(val id: String, val name: String, val isDir: Boolean, val size: Long) {
        fun uri(tree: Uri): Uri = DocumentsContract.buildDocumentUriUsingTree(tree, id)
    }

    val rootId: String = DocumentsContract.getTreeDocumentId(treeUri)

    fun uriOf(docId: String): Uri = DocumentsContract.buildDocumentUriUsingTree(treeUri, docId)

    fun list(parentId: String): List<Doc> {
        val uri = DocumentsContract.buildChildDocumentsUriUsingTree(treeUri, parentId)
        val cols = arrayOf(
            Document.COLUMN_DOCUMENT_ID, Document.COLUMN_DISPLAY_NAME,
            Document.COLUMN_MIME_TYPE, Document.COLUMN_SIZE,
        )
        val out = ArrayList<Doc>()
        resolver.query(uri, cols, null, null, null)?.use { c ->
            while (c.moveToNext()) {
                val id = c.getString(0) ?: continue
                val name = c.getString(1) ?: continue
                val mime = c.getString(2) ?: ""
                val size = if (c.isNull(3)) 0L else c.getLong(3)
                out.add(Doc(id, name, mime == Document.MIME_TYPE_DIR, size))
            }
        }
        return out
    }

    fun child(parentId: String, name: String): Doc? = list(parentId).firstOrNull { it.name == name }

    fun childIgnoreCase(parentId: String, name: String): Doc? =
        list(parentId).firstOrNull { it.name.equals(name, ignoreCase = true) }

    /** 하위 폴더를 이름 경로대로 찾고 없으면 만든다. */
    fun mkdirs(parentId: String, path: List<String>): String {
        var cur = parentId
        for (part in path) {
            val existing = child(cur, part)
            cur = if (existing != null) {
                require(existing.isDir) { "$part 는 폴더가 아닙니다" }
                existing.id
            } else {
                val created = DocumentsContract.createDocument(resolver, uriOf(cur), Document.MIME_TYPE_DIR, part)
                    ?: throw FileNotFoundException("폴더 생성 실패: $part")
                DocumentsContract.getDocumentId(created)
            }
        }
        return cur
    }

    /** 파일 쓰기 (있으면 내용 교체). "wt" 로 열어서 기존 내용이 남지 않게. */
    fun writeFile(parentId: String, name: String, src: File) {
        val existing = child(parentId, name)
        val uri = if (existing != null) {
            existing.uri(treeUri)
        } else {
            DocumentsContract.createDocument(resolver, uriOf(parentId), "application/octet-stream", name)
                ?: throw FileNotFoundException("파일 생성 실패: $name")
        }
        resolver.openOutputStream(uri, "wt")!!.use { out -> src.inputStream().use { it.copyTo(out) } }
    }

    fun writeFromUri(parentId: String, name: String, source: Uri) {
        val existing = child(parentId, name)
        val uri = existing?.uri(treeUri)
            ?: DocumentsContract.createDocument(resolver, uriOf(parentId), "application/octet-stream", name)
            ?: throw FileNotFoundException("파일 생성 실패: $name")
        resolver.openOutputStream(uri, "wt")!!.use { out ->
            resolver.openInputStream(source)!!.use { it.copyTo(out) }
        }
    }

    fun copyToFile(doc: Doc, dest: File) {
        dest.parentFile?.mkdirs()
        resolver.openInputStream(doc.uri(treeUri))!!.use { input ->
            dest.outputStream().use { input.copyTo(it) }
        }
    }

    fun readText(doc: Doc, maxBytes: Int = 256 * 1024): String =
        resolver.openInputStream(doc.uri(treeUri))!!.use { input ->
            val buf = input.readBytes()
            String(buf, 0, minOf(buf.size, maxBytes), Charsets.UTF_8)
        }

    fun delete(doc: Doc): Boolean = runCatching {
        DocumentsContract.deleteDocument(resolver, doc.uri(treeUri))
    }.getOrDefault(false)

    /** 재귀 순회. visitor 가 false 를 돌려주면 그 폴더로 내려가지 않는다. */
    fun walk(parentId: String, prefix: String, onDir: (Doc, String) -> Boolean, onFile: (Doc, String) -> Unit) {
        for (d in list(parentId)) {
            val rel = if (prefix.isEmpty()) d.name else "$prefix/${d.name}"
            if (d.isDir) {
                if (onDir(d, rel)) walk(d.id, rel, onDir, onFile)
            } else {
                onFile(d, rel)
            }
        }
    }
}
