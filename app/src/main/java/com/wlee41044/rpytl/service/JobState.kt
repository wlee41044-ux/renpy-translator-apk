package com.wlee41044.rpytl.service

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.atomic.AtomicBoolean

/** 서비스 ↔ 화면 공유 상태 (같은 프로세스). 영속 상태는 전부 DB 에 있다. */
object JobState {
    data class Running(val gameId: Long, val kind: String)

    private val _running = MutableStateFlow<Running?>(null)
    val running: StateFlow<Running?> = _running.asStateFlow()

    private val _log = MutableStateFlow<List<String>>(emptyList())
    val log: StateFlow<List<String>> = _log.asStateFlow()

    /** (gameId, done, total) */
    private val _progress = MutableStateFlow(Triple(0L, 0, 0))
    val progress: StateFlow<Triple<Long, Int, Int>> = _progress.asStateFlow()

    /** DB 가 바뀔 때마다 증가 → 화면이 통계를 다시 읽는다 */
    private val _dbVersion = MutableStateFlow(0)
    val dbVersion: StateFlow<Int> = _dbVersion.asStateFlow()

    val stopRequested = AtomicBoolean(false)

    private val fmt = SimpleDateFormat("HH:mm:ss", Locale.US)
    private const val MAX_LOG = 400

    fun log(msg: String) {
        val line = "${fmt.format(Date())} $msg"
        android.util.Log.i("rpytl", msg)
        synchronized(this) {
            val cur = _log.value
            _log.value = (if (cur.size >= MAX_LOG) cur.drop(cur.size - MAX_LOG + 1) else cur) + line
        }
    }

    fun setRunning(r: Running?) {
        _running.value = r
    }

    fun setProgress(gameId: Long, done: Int, total: Int) {
        _progress.value = Triple(gameId, done, total)
    }

    fun dbChanged() {
        _dbVersion.value = _dbVersion.value + 1
    }
}
