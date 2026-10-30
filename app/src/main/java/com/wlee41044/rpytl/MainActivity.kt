package com.wlee41044.rpytl

import android.Manifest
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.DocumentsContract
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.wlee41044.rpytl.apps.RenpyApps
import com.wlee41044.rpytl.saf.GameFolder
import com.wlee41044.rpytl.shizuku.ShizukuFs
import org.json.JSONObject
import rikka.shizuku.Shizuku
import com.wlee41044.rpytl.service.JobState
import com.wlee41044.rpytl.service.TranslateService
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        val engine = Engine(this)
        val prefs = Prefs(this)
        setContent {
            MaterialTheme(colorScheme = darkColorScheme()) {
                Surface(Modifier.fillMaxSize()) {
                    Box(Modifier.safeDrawingPadding()) { MainScreen(engine, prefs) }
                }
            }
        }
    }
}

private data class CharRow(val name: String, val lines: Int, val profile: String)

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun MainScreen(engine: Engine, prefs: Prefs) {
    val ctx = androidx.compose.ui.platform.LocalContext.current
    val scope = rememberCoroutineScope()
    var games by remember { mutableStateOf<List<GameInfo>>(emptyList()) }
    var selected by remember { mutableStateOf<Long?>(null) }
    var stats by remember { mutableStateOf(Stats.EMPTY) }
    val running by JobState.running.collectAsState()
    val log by JobState.log.collectAsState()
    val dbVersion by JobState.dbVersion.collectAsState()
    var showSettings by remember { mutableStateOf(false) }
    var found by remember { mutableStateOf<List<RenpyApps.App>?>(null) }
    var searching by remember { mutableStateOf(false) }
    var shizuku by remember { mutableStateOf(ShizukuFs.status()) }

    DisposableEffect(Unit) {
        val listener = Shizuku.OnRequestPermissionResultListener { _, _ -> shizuku = ShizukuFs.status() }
        val alive = Shizuku.OnBinderReceivedListener { shizuku = ShizukuFs.status() }
        val dead = Shizuku.OnBinderDeadListener { shizuku = ShizukuFs.status() }
        Shizuku.addRequestPermissionResultListener(listener)
        Shizuku.addBinderReceivedListenerSticky(alive)
        Shizuku.addBinderDeadListener(dead)
        onDispose {
            Shizuku.removeRequestPermissionResultListener(listener)
            Shizuku.removeBinderReceivedListener(alive)
            Shizuku.removeBinderDeadListener(dead)
        }
    }

    fun ui(msg: String) = JobState.log(msg)

    fun reload() {
        scope.launch {
            val list = withContext(Dispatchers.IO) { runCatching { engine.listGames() }.getOrElse { ui("DB 읽기 실패: ${it.message}"); emptyList() } }
            games = list
            if (selected == null || list.none { it.id == selected }) selected = list.firstOrNull()?.id
            stats = list.firstOrNull { it.id == selected }?.stats ?: Stats.EMPTY
        }
    }

    LaunchedEffect(dbVersion) { reload() }

    val notifPerm = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {}
    LaunchedEffect(Unit) {
        if (Build.VERSION.SDK_INT >= 33) notifPerm.launch(Manifest.permission.POST_NOTIFICATIONS)
    }

    val pickFolder = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri: Uri? ->
        if (uri == null) return@rememberLauncherForActivityResult
        scope.launch {
            val id = withContext(Dispatchers.IO) {
                runCatching {
                    ctx.contentResolver.takePersistableUriPermission(
                        uri, Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION,
                    )
                    val folder = GameFolder(ctx.contentResolver, uri)
                    if (!folder.looksLikeRenpy()) {
                        ui("경고: 고른 폴더(또는 그 안의 game/)에서 .rpa/.rpy/.rpyc 를 찾지 못했습니다")
                    }
                    val name = DocumentsContract.getTreeDocumentId(uri).substringAfterLast('/').substringAfterLast(':')
                        .ifBlank { "game" }
                    engine.addGame(name, uri.toString(), "korean")
                }.onFailure { ui("게임 추가 실패: ${it.message}") }.getOrNull()
            }
            if (id != null) {
                ui("게임 추가됨 (id=$id). '스캔'을 누르세요.")
                selected = id
                reload()
            }
        }
    }

    var copyingModel by remember { mutableStateOf(false) }
    val pickModel = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri: Uri? ->
        if (uri == null) return@rememberLauncherForActivityResult
        copyingModel = true
        scope.launch {
            // llama.cpp 는 파일 경로로 읽으므로 앱 전용 외부 저장소로 한 번 복사한다
            val path = withContext(Dispatchers.IO) {
                runCatching { copyModel(ctx, uri) { ui(it) } }.onFailure { ui("모델 복사 실패: ${it.message}") }.getOrNull()
            }
            if (path != null) {
                prefs.modelPath = path
                ui("모델 지정: $path")
            }
            copyingModel = false
        }
    }

    val pickFont = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri: Uri? ->
        val gid = selected
        if (uri != null && gid != null) {
            runCatching {
                ctx.contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
            prefs.setFontUri(gid, uri.toString())
            ui("폰트 지정됨. '출력 파일 생성' 때 tl 폴더로 복사합니다.")
        }
    }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(12.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Text("RenPy 번역기", fontWeight = FontWeight.Bold, fontSize = 20.sp)

        // ---- Shizuku (Android/data 에 번역 파일 쓰기용)
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                shizuku.label, Modifier.weight(1f), fontSize = 13.sp,
                color = if (shizuku == ShizukuFs.Status.READY) Color(0xFF80E080) else Color(0xFFFFB060),
            )
            if (shizuku == ShizukuFs.Status.NO_PERMISSION) {
                OutlinedButton(onClick = { ShizukuFs.requestPermission() }) { Text("권한 요청") }
            }
            OutlinedButton(onClick = { shizuku = ShizukuFs.status() }) { Text("새로고침") }
        }

        // ---- 설치된 Ren'Py 게임 찾기
        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            Button(enabled = !searching, onClick = {
                searching = true
                scope.launch {
                    val list = withContext(Dispatchers.IO) {
                        runCatching { RenpyApps.find(ctx) }.getOrElse { ui("앱 검색 실패: ${it.message}"); emptyList() }
                    }
                    found = list
                    searching = false
                    ui("설치된 Ren'Py 게임 ${list.size}개")
                }
            }) { Text(if (searching) "찾는 중…" else "설치된 게임 찾기") }
            OutlinedButton(onClick = { pickFolder.launch(null) }) { Text("폴더로 추가") }
        }
        found?.let { list ->
            val added = games.map { it.folderUri }.toSet()
            for (app in list) {
                val loc = TranslateService.APK_PREFIX + app.pkg
                Row(
                    Modifier.fillMaxWidth().background(Color(0xFF1A2420)).padding(6.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Column(Modifier.weight(1f)) {
                        Text(app.label)
                        Text("${app.pkg}  ${app.sizeMb}MB", fontSize = 11.sp)
                    }
                    if (loc in added) {
                        Text("추가됨", fontSize = 12.sp)
                    } else {
                        OutlinedButton(onClick = {
                            scope.launch {
                                val id = withContext(Dispatchers.IO) {
                                    runCatching { engine.addGame(app.label, loc, "korean") }
                                        .onFailure { ui("추가 실패: ${it.message}") }.getOrNull()
                                }
                                if (id != null) {
                                    ui("${app.label} 추가됨. '스캔'을 누르세요.")
                                    selected = id
                                    reload()
                                }
                            }
                        }) { Text("추가") }
                    }
                }
            }
        }

        // ---- 게임 목록
        HorizontalDivider()
        if (games.isEmpty()) Text("'설치된 게임 찾기'로 Ren'Py 게임 앱을 추가하세요.")
        for (g in games) {
            val sel = g.id == selected
            Column(
                Modifier.fillMaxWidth()
                    .background(if (sel) Color(0xFF2A3A55) else Color(0xFF1E1E1E))
                    .clickable { selected = g.id; stats = g.stats }
                    .padding(8.dp)
            ) {
                val kind = if (g.folderUri.startsWith(TranslateService.APK_PREFIX)) "앱" else "폴더"
                Text("${g.name}  [${g.language}] ($kind)", fontWeight = if (sel) FontWeight.Bold else FontWeight.Normal)
                Text(
                    "완료 ${g.stats.done}/${g.stats.total}  검수 ${g.stats.review}  미사용 ${g.stats.unused}",
                    fontSize = 12.sp,
                )
            }
        }

        val gid = selected
        val game = games.firstOrNull { it.id == gid }
        if (game != null && gid != null) {
            HorizontalDivider()
            val busy = running != null
            val runText = running?.let { "작업 중: ${it.kind} (게임 ${it.gameId}) — 지금 누른 작업은 대기열에 추가되어 순서대로 실행됩니다" } ?: "대기"
            Text(runText, fontSize = 12.sp)
            Text(
                "전체 ${stats.total}  완료 ${stats.done}  남은 ${stats.pending}  검수필요 ${stats.review}  미사용 ${stats.unused}",
            )
            LinearProgressIndicator(progress = { stats.progress.toFloat() }, modifier = Modifier.fillMaxWidth())
            Text("진행률 ${"%.1f".format(stats.progress * 100)}%", fontSize = 12.sp)

            FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                Button(onClick = { TranslateService.start(ctx, TranslateService.ACTION_SCAN, gid) }) {
                    Text("스캔")
                }
                Button(onClick = { TranslateService.start(ctx, TranslateService.ACTION_TRANSLATE, gid) }) {
                    Text(if (stats.done > 0) "번역 이어하기" else "번역 시작")
                }
                OutlinedButton(enabled = busy, onClick = { TranslateService.pause(ctx) }) { Text("일시정지") }
                Button(onClick = { TranslateService.start(ctx, TranslateService.ACTION_EXPORT, gid) }) {
                    Text("출력 파일 생성")
                }
                OutlinedButton(enabled = !busy, onClick = {
                    TranslateService.start(ctx, TranslateService.ACTION_UNAPPLY, gid)
                }) { Text("번역 적용 해제(게임 복구)") }
                OutlinedButton(onClick = {
                    TranslateService.start(ctx, TranslateService.ACTION_ANALYZE, gid)
                }) { Text("캐릭터 분석") }
                Button(onClick = {
                    TranslateService.start(ctx, TranslateService.ACTION_ANALYZE_TRANSLATE, gid)
                }) { Text("분석 후 번역(밤새)") }
                OutlinedButton(enabled = !busy && stats.review > 0, onClick = {
                    TranslateService.start(ctx, TranslateService.ACTION_TRANSLATE, gid, retryReview = true)
                }) { Text("검수필요 재번역") }
                OutlinedButton(enabled = stats.review > 0, onClick = {
                    scope.launch {
                        val cats = withContext(Dispatchers.IO) { engine.reviewSummary(gid) }
                        ui("— 검수필요 사유별 —")
                        for (i in 0 until cats.length()) {
                            val c = cats.getJSONObject(i)
                            ui("● ${c.getString("name")}: ${c.getInt("count")}줄")
                            val samples = c.getJSONArray("samples")
                            for (j in 0 until samples.length()) {
                                val o = samples.getJSONObject(j)
                                ui("  원문: ${o.optString("source")}\n  번역: ${o.optString("translation")}")
                            }
                        }
                    }
                }) { Text("검수필요 보기") }
            }

            // ---- 엔진 선택
            var engineSel by remember { mutableStateOf(prefs.engine) }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("엔진: ")
                RadioButton(selected = engineSel == "google", onClick = { engineSel = "google"; prefs.engine = "google" })
                Text("Google")
                RadioButton(selected = engineSel == "local", onClick = { engineSel = "local"; prefs.engine = "local" })
                Text("로컬")
                RadioButton(selected = engineSel == "gemini", onClick = { engineSel = "gemini"; prefs.engine = "gemini" })
                Text("Gemini")
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                RadioButton(selected = engineSel == "hybrid", onClick = { engineSel = "hybrid"; prefs.engine = "hybrid" })
                Text("혼합: 대사 = Gemini, 나머지(아이템 설명·UI 글자) = Google, 동시에")
            }
            if (engineSel == "gemini" || engineSel == "hybrid") {
                Text(
                    "Gemini: 게임 대사가 Google 서버로 전송됩니다. 설정에서 API 키를 입력하세요.",
                    fontSize = 12.sp, color = Color(0xFFFFB060),
                )
            }

            // ---- 게임별: 언어, 폰트
            var lang by remember(gid) { mutableStateOf(game.language) }
            Row(verticalAlignment = Alignment.CenterVertically) {
                OutlinedTextField(
                    value = lang, onValueChange = { lang = it.trim() }, singleLine = true,
                    label = { Text("tl 언어 이름") }, modifier = Modifier.weight(1f),
                )
                OutlinedButton(enabled = !busy && lang != game.language, onClick = {
                    scope.launch {
                        withContext(Dispatchers.IO) {
                            runCatching { engine.setLanguage(gid, lang) }.onFailure { ui("언어 변경 실패: ${it.message}") }
                        }
                        reload()
                    }
                }) { Text("적용") }
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(if (prefs.fontUri(gid) != null) "폰트: 지정됨" else "폰트: 게임 기본", Modifier.weight(1f), fontSize = 13.sp)
                OutlinedButton(onClick = { pickFont.launch(arrayOf("*/*")) }) { Text("폰트 선택") }
                OutlinedButton(onClick = { prefs.setFontUri(gid, null); ui("폰트 지정 해제") }) { Text("해제") }
            }

            OutlinedButton(onClick = { showSettings = !showSettings }) {
                Text(if (showSettings) "설정 닫기" else "설정 열기")
            }
            if (showSettings) {
                SettingsPanel(prefs, engine, scope) { ui(it) }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        "모델: " + (prefs.modelPath.substringAfterLast('/').ifBlank { "없음" }),
                        Modifier.weight(1f), fontSize = 13.sp,
                    )
                    OutlinedButton(enabled = !copyingModel, onClick = { pickModel.launch(arrayOf("*/*")) }) {
                        Text(if (copyingModel) "복사 중…" else "GGUF 모델 선택")
                    }
                }
            }

            // ---- 번역 지우기 (로컬 모델로 다시 번역하고 싶을 때)
            var resetCounts by remember(gid) { mutableStateOf<JSONObject?>(null) }
            OutlinedButton(enabled = !busy && stats.done + stats.review > 0, onClick = {
                scope.launch {
                    resetCounts = withContext(Dispatchers.IO) { runCatching { engine.engineCounts(gid) }.getOrNull() }
                }
            }) { Text("번역 지우기 (다시 번역)", color = Color(0xFFFFB060)) }
            resetCounts?.let { counts ->
                val g = counts.optInt("google")
                val total = counts.keys().asSequence().sumOf { counts.optInt(it) }
                fun doReset(engineName: String?) {
                    resetCounts = null
                    scope.launch {
                        val n = withContext(Dispatchers.IO) {
                            runCatching { engine.resetTranslations(gid, engineName) }
                                .onFailure { ui("번역 지우기 실패: ${it.message}") }.getOrDefault(0)
                        }
                        ui("번역 ${n}줄을 지우고 대기로 되돌림 (지우기 전 DB 백업됨). '번역 시작'으로 다시 번역하세요.")
                        JobState.dbChanged()
                    }
                }
                AlertDialog(
                    onDismissRequest = { resetCounts = null },
                    title = { Text("번역 지우기") },
                    text = {
                        Text(
                            "현재 번역: Google ${g}줄, 전체 ${total}줄.\n" +
                                "지운 줄은 '대기'로 돌아가 다음 번역에서 다시 번역됩니다. 캐릭터 분석은 유지됩니다.\n" +
                                "이미 게임에 넣은 번역 파일은 '출력 파일 생성'을 다시 할 때까지 그대로입니다.",
                        )
                    },
                    confirmButton = {
                        TextButton(onClick = { doReset("google") }) { Text("Google 번역만 지우기") }
                    },
                    dismissButton = {
                        Row {
                            TextButton(onClick = { doReset(null) }) { Text("전부 지우기") }
                            TextButton(onClick = { resetCounts = null }) { Text("취소") }
                        }
                    },
                )
            }

            // ---- 캐릭터 분석 보기/수정
            var showChars by remember(gid) { mutableStateOf(false) }
            var chars by remember(gid) { mutableStateOf<List<CharRow>>(emptyList()) }
            val edits = remember(gid) { mutableStateMapOf<String, String>() }
            fun loadChars() {
                scope.launch {
                    chars = withContext(Dispatchers.IO) {
                        runCatching {
                            val a = engine.characters(gid)
                            (0 until a.length()).map {
                                val o = a.getJSONObject(it)
                                CharRow(o.getString("name"), o.optInt("lines"), o.optString("profile"))
                            }
                        }.getOrElse { e -> ui("캐릭터 목록 읽기 실패: ${e.message}"); emptyList() }
                    }
                }
            }
            LaunchedEffect(dbVersion, showChars) { if (showChars) loadChars() }
            OutlinedButton(onClick = { showChars = !showChars }) {
                Text(if (showChars) "캐릭터 분석 닫기" else "캐릭터 분석 보기/수정")
            }
            if (showChars) {
                Text(
                    "분석 결과를 고치면 번역할 때 그 내용이 쓰입니다. 고친 것은 다시 분석해도 유지됩니다. 비우면 삭제.",
                    fontSize = 12.sp,
                )
                if (chars.isEmpty()) Text("아직 분석 결과가 없습니다. '캐릭터 분석'을 먼저 실행하세요 (로컬 모델 필요).", fontSize = 12.sp)
                for (c in chars) {
                    val label = if (c.name == "__story__") "작품 요약" else "${c.name} (${c.lines}줄)"
                    OutlinedTextField(
                        value = edits[c.name] ?: c.profile,
                        onValueChange = { edits[c.name] = it },
                        label = { Text(label, fontSize = 12.sp) },
                        minLines = 2,
                        modifier = Modifier.fillMaxWidth(),
                    )
                }
                Button(enabled = edits.isNotEmpty(), onClick = {
                    scope.launch {
                        withContext(Dispatchers.IO) {
                            for ((k, v) in edits.toMap()) runCatching { engine.setProfile(gid, k, v) }
                        }
                        edits.clear()
                        ui("캐릭터 분석 수정 저장됨")
                        loadChars()
                    }
                }) { Text("수정 저장") }
            }

            OutlinedButton(enabled = !busy, onClick = {
                scope.launch {
                    withContext(Dispatchers.IO) { engine.deleteGame(gid) }
                    ui("목록에서 제거 (게임 파일은 그대로)")
                    selected = null
                    reload()
                }
            }) { Text("목록에서 제거", color = Color(0xFFFF8080)) }
        }

        // ---- 로그
        HorizontalDivider()
        Text("로그", fontWeight = FontWeight.Bold)
        val listState = rememberLazyListState()
        LaunchedEffect(log.size) { if (log.isNotEmpty()) listState.scrollToItem(log.size - 1) }
        LazyColumn(
            state = listState,
            modifier = Modifier.fillMaxWidth().height(320.dp).background(Color(0xFF111111)).padding(4.dp),
        ) {
            items(log) { line -> Text(line, fontFamily = FontFamily.Monospace, fontSize = 11.sp) }
        }
    }
}

@Composable
private fun SettingsPanel(prefs: Prefs, engine: Engine, scope: kotlinx.coroutines.CoroutineScope, log: (String) -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        // ---- Gemini API
        Text("Gemini API", fontWeight = FontWeight.Bold, fontSize = 13.sp)
        var keyText by remember { mutableStateOf(prefs.geminiKey) }
        OutlinedTextField(
            value = keyText, onValueChange = { keyText = it; prefs.geminiKey = it }, singleLine = true,
            label = { Text("API 키 (이 앱 안에만 저장, 화면에 가려짐)", fontSize = 12.sp) },
            visualTransformation = PasswordVisualTransformation(),
            modifier = Modifier.fillMaxWidth(),
        )
        TextPref("모델 이름 (기본 gemini-flash-latest)", prefs.geminiModel) { prefs.geminiModel = it.ifBlank { "gemini-flash-latest" } }
        IntPref("Gemini 분당 요청 수 (무료 등급은 낮게, 기본 10)", prefs.geminiRpm) { prefs.geminiRpm = it.coerceIn(1, 1000) }
        IntPref("Gemini 요청당 줄 수 (기본 20)", prefs.geminiBatch) { prefs.geminiBatch = it.coerceIn(1, 60) }
        OutlinedButton(onClick = {
            scope.launch {
                log("Gemini 키 확인/모델 목록 요청 중…")
                val res = withContext(Dispatchers.IO) { runCatching { engine.geminiModels(prefs.geminiKey) } }
                res.onSuccess { names ->
                    log("키 확인 OK. 쓸 수 있는 모델 ${names.size}개: " + names.take(30).joinToString(", "))
                }.onFailure { log("Gemini 확인 실패: ${it.message}") }
            }
        }) { Text("키 확인 / 모델 목록 보기") }
        HorizontalDivider()
        TextPref("원문 언어 (Google 코드, auto=자동)", prefs.sourceLang) { prefs.sourceLang = it.ifBlank { "auto" } }
        TextPref("번역 언어 코드 (비우면 tl 언어에서 자동, korean→ko)", prefs.targetCode) { prefs.targetCode = it }
        // ---- Google 번역 속도: 기본값은 요청 제한(429)이 가장 덜 걸렸던 값. 더 빠르게 올리면 경고.
        var workers by remember { mutableStateOf(prefs.googleWorkers) }
        var intervalMs by remember { mutableStateOf(prefs.googleDelayMs) }
        var batch by remember { mutableStateOf(prefs.googleBatch) }
        var speedTick by remember { mutableStateOf(0) }
        androidx.compose.runtime.key(speedTick) {
            IntPref("Google 동시 요청 수 (기본 ${Prefs.DEFAULT_GOOGLE_WORKERS}, 1~8)", workers) {
                prefs.googleWorkers = it.coerceIn(1, 8); workers = prefs.googleWorkers
            }
            IntPref("Google 요청 시작 간격 ms (기본 ${Prefs.DEFAULT_GOOGLE_INTERVAL_MS}, 전체 공통, 낮을수록 빠름)", intervalMs) {
                prefs.googleDelayMs = it.coerceIn(100, 60000); intervalMs = prefs.googleDelayMs
            }
            IntPref("Google 요청당 줄 수 (기본 ${Prefs.DEFAULT_GOOGLE_BATCH}, 클수록 빠름)", batch) {
                prefs.googleBatch = it.coerceIn(1, 100); batch = prefs.googleBatch
            }
        }
        val faster = workers > Prefs.DEFAULT_GOOGLE_WORKERS || intervalMs < Prefs.DEFAULT_GOOGLE_INTERVAL_MS ||
            batch > Prefs.DEFAULT_GOOGLE_BATCH
        if (faster) {
            Text(
                "⚠ 기본보다 빠른 설정입니다: Google 요청 제한(429)이 걸릴 수 있습니다. " +
                    "걸리면 자동으로 느려지고, 성공 없이 3번 연속 걸리면 번역이 자동 일시정지됩니다.",
                color = Color(0xFFFFB060), fontSize = 12.sp,
            )
        }
        val isDefault = workers == Prefs.DEFAULT_GOOGLE_WORKERS && intervalMs == Prefs.DEFAULT_GOOGLE_INTERVAL_MS &&
            batch == Prefs.DEFAULT_GOOGLE_BATCH
        OutlinedButton(enabled = !isDefault, onClick = {
            prefs.resetGoogleSpeed()
            workers = prefs.googleWorkers; intervalMs = prefs.googleDelayMs; batch = prefs.googleBatch
            speedTick++
        }) { Text("속도를 기본값으로") }
        BoolPref("Google 실패 시 로컬 모델로 전환", prefs.fallbackLocal) { prefs.fallbackLocal = it }
        IntPref("로컬: 배치 크기 (발열 시 줄이기)", prefs.localBatch) { prefs.localBatch = it.coerceIn(1, 32) }
        IntPref("로컬: 쓰레드 수 (발열 시 줄이기)", prefs.localThreads) { prefs.localThreads = it.coerceIn(1, 8) }
        IntPref("로컬: 컨텍스트 길이 (메모리 부족 시 줄이기)", prefs.localCtx) { prefs.localCtx = it.coerceIn(512, 32768) }
        IntPref("로컬: 배치 사이 쉬는 시간 ms (발열 조절)", prefs.localCooldownMs) { prefs.localCooldownMs = it.coerceIn(0, 600000) }
        BoolPref("로컬: 생각(thinking) 모드 끄기 (Qwen3)", prefs.localNoThink) { prefs.localNoThink = it }
        TextPref("로컬: GGUF 모델 파일 경로 (아래 버튼으로 선택 권장)", prefs.modelPath) { prefs.modelPath = it }
        BoolPref("스캔: 코드 속 문자열도 번역 (아이템/스킬 설명, 상태창)", prefs.pyStrings) { prefs.pyStrings = it }
        BoolPref("로컬 모델: 캐릭터 분석/화자/앞 대사를 번역에 사용", prefs.useProfiles) { prefs.useProfiles = it }
        BoolPref("스캔: 게임에 이미 있는 다른 번역이 덮는 줄은 건너뛰기", prefs.skipExisting) { prefs.skipExisting = it }
        BoolPref("언어 강제 (config.language)", prefs.forceLanguage) { prefs.forceLanguage = it }
        BoolPref(
            "폰트: 한글만 한글 폰트로 (끄면 게임 폰트를 통째로 교체)", prefs.fontMode == "fallback",
        ) { prefs.fontMode = if (it) "fallback" else "replace" }
    }
}

@Composable
private fun TextPref(label: String, initial: String, save: (String) -> Unit) {
    var v by remember { mutableStateOf(initial) }
    OutlinedTextField(
        value = v, onValueChange = { v = it; save(it.trim()) }, singleLine = true,
        label = { Text(label, fontSize = 12.sp) }, modifier = Modifier.fillMaxWidth(),
    )
}

@Composable
private fun IntPref(label: String, initial: Int, save: (Int) -> Unit) {
    var v by remember { mutableStateOf(initial.toString()) }
    OutlinedTextField(
        value = v, onValueChange = { s -> v = s.filter { it.isDigit() }.take(6); v.toIntOrNull()?.let(save) },
        singleLine = true, label = { Text(label, fontSize = 12.sp) },
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
        modifier = Modifier.fillMaxWidth(),
    )
}

@Composable
private fun BoolPref(label: String, initial: Boolean, save: (Boolean) -> Unit) {
    var v by remember { mutableStateOf(initial) }
    Row(verticalAlignment = Alignment.CenterVertically) {
        Checkbox(checked = v, onCheckedChange = { v = it; save(it) })
        Text(label, fontSize = 13.sp)
    }
}


/** SAF 로 고른 GGUF 를 Android/data/<이 앱>/files/models/ 로 복사하고 경로를 돌려준다. */
private fun copyModel(ctx: android.content.Context, uri: Uri, log: (String) -> Unit): String {
    val name = ctx.contentResolver.query(uri, arrayOf(android.provider.OpenableColumns.DISPLAY_NAME), null, null, null)
        ?.use { c -> if (c.moveToFirst()) c.getString(0) else null } ?: "model.gguf"
    require(name.lowercase().endsWith(".gguf")) { "GGUF 파일이 아닙니다: $name" }
    val dir = java.io.File(ctx.getExternalFilesDir(null) ?: ctx.filesDir, "models").apply { mkdirs() }
    val dest = java.io.File(dir, name.replace('/', '_'))
    val tmp = java.io.File(dir, "$name.part")
    log("모델 복사 시작: $name")
    ctx.contentResolver.openInputStream(uri)!!.use { input ->
        tmp.outputStream().use { out ->
            val buf = ByteArray(1 shl 20)
            var total = 0L
            var nextLog = 256L shl 20
            while (true) {
                val n = input.read(buf)
                if (n < 0) break
                out.write(buf, 0, n)
                total += n
                if (total >= nextLog) {
                    log("  ${total shr 20}MB")
                    nextLog += 256L shl 20
                }
            }
        }
    }
    if (dest.exists()) dest.delete()
    check(tmp.renameTo(dest)) { "이름 바꾸기 실패" }
    // 예전에 복사한 다른 모델은 지워서 공간 확보
    dir.listFiles()?.filter { it != dest }?.forEach { it.delete() }
    log("모델 복사 완료: ${dest.length() shr 20}MB")
    return dest.absolutePath
}
