package com.wlee41044.rpytl.shizuku

import android.content.Context
import android.os.ParcelFileDescriptor
import android.os.Process
import androidx.annotation.Keep
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import kotlin.system.exitProcess

/**
 * Shizuku 가 shell(uid 2000) 권한으로 띄우는 별도 프로세스에서 실행된다.
 * 안전을 위해 외부 저장소의 Android/data 아래 경로만 허용한다.
 */
class FileService() : IFileService.Stub() {

    @Keep
    constructor(@Suppress("UNUSED_PARAMETER") context: Context) : this()

    private fun checked(path: String): File {
        val f = File(path)
        require(f.isAbsolute && !path.split('/').contains("..")) { "bad path: $path" }
        require(ALLOWED.any { path.startsWith(it) }) { "path not allowed: $path" }
        return f
    }

    override fun destroy() {
        exitProcess(0)
    }

    override fun uid(): Int = Process.myUid()

    override fun list(dir: String): String {
        val root = checked(dir)
        val out = JSONArray()
        if (!root.isDirectory) return out.toString()
        root.walkTopDown().filter { it.isFile }.forEach {
            out.put(JSONObject().put("p", it.relativeTo(root).path).put("s", it.length()))
        }
        return out.toString()
    }

    override fun exists(path: String): Boolean = checked(path).exists()

    override fun mkdirs(dir: String) {
        val f = checked(dir)
        if (!f.isDirectory && !f.mkdirs()) throw IllegalStateException("mkdirs failed: $dir")
    }

    override fun write(path: String, src: ParcelFileDescriptor) {
        val f = checked(path)
        f.parentFile?.let { if (!it.isDirectory) it.mkdirs() }
        val tmp = File(f.parentFile, ".${f.name}.tmp")
        ParcelFileDescriptor.AutoCloseInputStream(src).use { input ->
            FileOutputStream(tmp).use { out ->
                input.copyTo(out)
                out.fd.sync()
            }
        }
        if (!tmp.renameTo(f)) {
            tmp.delete()
            throw IllegalStateException("rename failed: $path")
        }
    }

    override fun remove(path: String): Boolean = checked(path).delete()

    override fun openRead(path: String): ParcelFileDescriptor =
        ParcelFileDescriptor.open(checked(path), ParcelFileDescriptor.MODE_READ_ONLY)

    companion object {
        val ALLOWED = listOf("/storage/emulated/0/Android/data/", "/sdcard/Android/data/")
    }
}
