package com.wlee41044.rpytl

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.chaquo.python.Python

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val engineInfo = runCatching {
            Python.getInstance().getModule("rpytl").callAttr("engine_info").toString()
        }.getOrElse { "엔진 로드 실패: ${it.message}" }

        setContent {
            MaterialTheme {
                Surface {
                    Column(Modifier.padding(16.dp)) {
                        Text("RenPy 번역기 (뼈대)")
                        Text(engineInfo)
                    }
                }
            }
        }
    }
}
