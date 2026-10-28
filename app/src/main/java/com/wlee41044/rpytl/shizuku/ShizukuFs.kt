package com.wlee41044.rpytl.shizuku

import android.content.ComponentName
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import com.wlee41044.rpytl.BuildConfig
import rikka.shizuku.Shizuku
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** Shizuku 상태 확인, 권한 요청, FileService 연결. */
object ShizukuFs {
    enum class Status(val label: String) {
        NOT_RUNNING("Shizuku 꺼짐 (Shizuku 앱에서 시작하세요)"),
        OLD_VERSION("Shizuku 버전이 너무 낮음 (v11 이상 필요)"),
        NO_PERMISSION("Shizuku 권한 없음"),
        READY("Shizuku 연결됨"),
    }

    private const val REQUEST_CODE = 41
    private val main = Handler(Looper.getMainLooper())

    @Volatile
    private var service: IFileService? = null

    private val args by lazy {
        Shizuku.UserServiceArgs(ComponentName(BuildConfig.APPLICATION_ID, FileService::class.java.name))
            .daemon(false)
            .processNameSuffix("fs")
            .debuggable(BuildConfig.DEBUG)
            .version(BuildConfig.VERSION_CODE)
    }

    fun status(): Status = try {
        when {
            !Shizuku.pingBinder() -> Status.NOT_RUNNING
            Shizuku.isPreV11() -> Status.OLD_VERSION
            Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED -> Status.READY
            else -> Status.NO_PERMISSION
        }
    } catch (t: Throwable) {
        Status.NOT_RUNNING
    }

    fun requestPermission() {
        if (Shizuku.pingBinder() && !Shizuku.isPreV11()) Shizuku.requestPermission(REQUEST_CODE)
    }

    private var latch: CountDownLatch? = null

    private val connection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            if (binder != null && binder.pingBinder()) service = IFileService.Stub.asInterface(binder)
            latch?.countDown()
        }

        override fun onServiceDisconnected(name: ComponentName?) {
            service = null
        }
    }

    /** 백그라운드 스레드에서 호출. 연결될 때까지 기다린다. */
    @Synchronized
    fun get(timeoutSec: Long = 20): IFileService {
        service?.takeIf { it.asBinder().pingBinder() }?.let { return it }
        val st = status()
        check(st == Status.READY) { st.label }
        val l = CountDownLatch(1)
        latch = l
        main.post { runCatching { Shizuku.bindUserService(args, connection) }.onFailure { l.countDown() } }
        if (!l.await(timeoutSec, TimeUnit.SECONDS)) throw IllegalStateException("Shizuku 서비스 연결 시간 초과")
        return service ?: throw IllegalStateException("Shizuku 서비스 연결 실패")
    }

    /** 게임이 읽는 외부 game 폴더 (Ren'Py: ANDROID_PUBLIC/game) */
    fun gameDir(pkg: String) = "/storage/emulated/0/Android/data/$pkg/files/game"
    fun filesDir(pkg: String) = "/storage/emulated/0/Android/data/$pkg/files"
}
