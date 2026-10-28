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
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
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
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.wlee41044.rpytl.saf.GameFolder
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
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("RenPy 번역기", fontWeight = FontWeight.Bold, fontSize = 20.sp, modifier = Modifier.weight(1f))
            OutlinedButton(onClick = { pickFolder.launch(null) }) { Text("게임 추가") }
        }

        // ---- 게임 목록
        if (games.isEmpty()) Text("게임 폴더를 추가하세요 (game 폴더 또는 그 상위 폴더).")
        for (g in games) {
            val sel = g.id == selected
            Column(
                Modifier.fillMaxWidth()
                    .background(if (sel) Color(0xFF2A3A55) else Color(0xFF1E1E1E))
                    .clickable { selected = g.id; stats = g.stats }
                    .padding(8.dp)
            ) {
                Text("${g.name}  [${g.language}]", fontWeight = if (sel) FontWeight.Bold else FontWeight.Normal)
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
            val runText = running?.let { "작업 중: ${it.kind} (게임 ${it.gameId})" } ?: "대기"
            Text(runText, fontSize = 12.sp)
            Text(
                "전체 ${stats.total}  완료 ${stats.done}  남은 ${stats.pending}  검수필요 ${stats.review}  미사용 ${stats.unused}",
            )
            LinearProgressIndicator(progress = { stats.progress.toFloat() }, modifier = Modifier.fillMaxWidth())
            Text("진행률 ${"%.1f".format(stats.progress * 100)}%", fontSize = 12.sp)

            FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                Button(enabled = !busy, onClick = { TranslateService.start(ctx, TranslateService.ACTION_SCAN, gid) }) {
                    Text("스캔")
                }
                Button(enabled = !busy, onClick = { TranslateService.start(ctx, TranslateService.ACTION_TRANSLATE, gid) }) {
                    Text(if (stats.done > 0) "번역 이어하기" else "번역 시작")
                }
                OutlinedButton(enabled = busy, onClick = { TranslateService.pause(ctx) }) { Text("일시정지") }
                Button(enabled = !busy, onClick = { TranslateService.start(ctx, TranslateService.ACTION_EXPORT, gid) }) {
                    Text("출력 파일 생성")
                }
                OutlinedButton(enabled = !busy && stats.review > 0, onClick = {
                    TranslateService.start(ctx, TranslateService.ACTION_TRANSLATE, gid, retryReview = true)
                }) { Text("검수필요 재번역") }
                OutlinedButton(enabled = stats.review > 0, onClick = {
                    scope.launch {
                        val arr = withContext(Dispatchers.IO) { engine.reviewList(gid) }
                        ui("— 검수필요 ${arr.length()}개 (최대 200) —")
                        for (i in 0 until arr.length()) {
                            val o = arr.getJSONObject(i)
                            ui("원문: ${o.optString("source")}\n번역: ${o.optString("translation")}\n사유: ${o.optString("note")}")
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
                Text("로컬 모델")
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
            if (showSettings) SettingsPanel(prefs)

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
private fun SettingsPanel(prefs: Prefs) {
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        TextPref("원문 언어 (Google 코드, auto=자동)", prefs.sourceLang) { prefs.sourceLang = it.ifBlank { "auto" } }
        TextPref("번역 언어 코드 (비우면 tl 언어에서 자동, korean→ko)", prefs.targetCode) { prefs.targetCode = it }
        IntPref("Google 요청 간격 ms", prefs.googleDelayMs) { prefs.googleDelayMs = it.coerceIn(200, 60000) }
        IntPref("Google 한 요청당 줄 수", prefs.googleBatch) { prefs.googleBatch = it.coerceIn(1, 100) }
        BoolPref("Google 실패 시 로컬 모델로 전환", prefs.fallbackLocal) { prefs.fallbackLocal = it }
        IntPref("로컬: 배치 크기 (발열 시 줄이기)", prefs.localBatch) { prefs.localBatch = it.coerceIn(1, 32) }
        IntPref("로컬: 쓰레드 수 (발열 시 줄이기)", prefs.localThreads) { prefs.localThreads = it.coerceIn(1, 8) }
        TextPref("로컬: GGUF 모델 파일 경로", prefs.modelPath) { prefs.modelPath = it }
        BoolPref("언어 강제 (config.language)", prefs.forceLanguage) { prefs.forceLanguage = it }
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
