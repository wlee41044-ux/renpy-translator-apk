package com.wlee41044.rpytl.apps

import android.content.Context
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import java.io.File
import java.util.zip.ZipFile

/** 설치된 앱 중 Ren'Py 게임 찾기. APK 는 다른 앱도 권한 없이 읽을 수 있다. */
object RenpyApps {
    data class App(val pkg: String, val label: String, val apks: List<String>, val sizeMb: Long)

    fun apkPaths(ai: ApplicationInfo): List<String> = listOf(ai.sourceDir) + (ai.splitSourceDirs?.toList() ?: emptyList())

    fun info(context: Context, pkg: String): ApplicationInfo =
        context.packageManager.getApplicationInfo(pkg, 0)

    fun isRenpy(ai: ApplicationInfo): Boolean {
        if (File(ai.nativeLibraryDir ?: "", "librenpython.so").exists()) return true
        return apkPaths(ai).any { path ->
            runCatching {
                ZipFile(path).use { z ->
                    z.entries().asSequence().any {
                        it.name.startsWith("assets/x-game/") || it.name.startsWith("assets/x-renpy/") ||
                            it.name.startsWith("lib/arm64-v8a/librenpython")
                    }
                }
            }.getOrDefault(false)
        }
    }

    fun find(context: Context): List<App> {
        val pm = context.packageManager
        return pm.getInstalledApplications(PackageManager.GET_META_DATA)
            .filter { it.flags and ApplicationInfo.FLAG_SYSTEM == 0 && it.packageName != context.packageName }
            .filter { isRenpy(it) }
            .map { ai ->
                val apks = apkPaths(ai)
                App(ai.packageName, pm.getApplicationLabel(ai).toString(), apks,
                    apks.sumOf { File(it).length() } / (1024 * 1024))
            }
            .sortedBy { it.label.lowercase() }
    }
}
