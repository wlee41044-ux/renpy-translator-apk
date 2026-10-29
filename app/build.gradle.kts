plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("com.chaquo.python")
}

android {
    namespace = "com.wlee41044.rpytl"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.wlee41044.rpytl"
        minSdk = 26
        targetSdk = 36
        versionCode = 18
        versionName = "0.9.4"

        ndk {
            // 갤럭시 S24 = arm64-v8a 만 (llama.cpp 빌드 시간/APK 크기 절약)
            abiFilters += listOf("arm64-v8a")
        }
        externalNativeBuild {
            cmake {
                arguments += listOf("-DCMAKE_BUILD_TYPE=Release")
            }
        }
    }

    signingConfigs {
        // 저장소에 고정된 debug 키를 쓴다. CI 빌드마다 키가 바뀌면 폰에서 덮어쓰기 설치가 안 되기 때문.
        // (debug 전용 키, 비밀 아님)
        getByName("debug") {
            storeFile = file("debug.keystore")
            storePassword = "android"
            keyAlias = "androiddebugkey"
            keyPassword = "android"
        }
    }

    buildTypes {
        debug {
            signingConfig = signingConfigs.getByName("debug")
        }
        release {
            isMinifyEnabled = false
            // 개인용: release 도 같은 키로 서명해서 debug 위에 덮어 설치 가능하게.
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    // llama.cpp (third_party/llama.cpp 서브모듈) + JNI 래퍼
    ndkVersion = "27.2.12479018"
    externalNativeBuild {
        cmake {
            path("src/main/cpp/CMakeLists.txt")
            version = "3.22.1"
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        compose = true
        aidl = true
        buildConfig = true
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

chaquopy {
    defaultConfig {
        version = "3.12"
    }
    sourceSets {
        getByName("main") {
            // 엔진은 순수 Python 패키지로 engine/src 에 두고 PC 에서 pytest 로 테스트한다.
            srcDir("../engine/src")
        }
    }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2025.08.00")
    implementation(composeBom)
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.activity:activity-compose:1.10.1")
    implementation("androidx.core:core-ktx:1.16.0")
    implementation("androidx.documentfile:documentfile:1.1.0")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.9.2")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")
    // Android/data/<게임>/files/game 에 번역 파일을 쓰기 위한 shell 권한 (Shizuku 앱 필요)
    implementation("dev.rikka.shizuku:api:13.1.5")
    implementation("dev.rikka.shizuku:provider:13.1.5")
}
