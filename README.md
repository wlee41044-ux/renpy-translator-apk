# RenPy Translator APK (개인용)

Ren'Py 게임 폴더를 폰에서 직접 스캔해 `game/tl/<언어>/` 번역 파일을 만들어 주는 Android 앱.
원본 스크립트는 건드리지 않는다. 게임 업데이트 시 바뀐 줄만 다시 번역한다.

- 대상: Android 8.0+ (minSdk 26), 주 테스트 기기 갤럭시 S24
- 구성: Kotlin + Jetpack Compose(최소 UI) + [Chaquopy](https://chaquo.com/chaquopy/) 17.2 (Python 3.12)
- 핵심 엔진: `engine/` 의 순수 Python 패키지 `rpytl` (PC 에서 `pytest` 로 테스트)

구조와 DB 스키마는 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 참고.

## 진행 상황

- [x] 1단계: 뼈대 + GitHub Actions 빌드
- [x] 2단계: Python 엔진 (추출 → 파싱 → 태그 보호 → DB → diff → tl 생성) + pytest
- [x] 3단계: Google 번역 + 번역 큐/이어하기 + Compose UI + Foreground Service
- [ ] 4단계: 로컬 모델 (llama.cpp GGUF)

## 폰만으로 빌드/설치

1. 이 저장소에 push 하면 Actions 가 debug APK 를 빌드한다.
2. GitHub 앱/웹 → Actions → 최신 `build` 실행 → Artifacts 의 `rpytl-debug-apk` 다운로드
   (zip 안에 APK). 로그인 필요.
3. `v0.1.0` 같은 태그를 push 하면 Releases 에 APK 가 첨부된다 (로그인 없이 다운로드 가능).

APK 는 저장소의 고정 debug 키(`app/debug.keystore`, 비밀 아님)로 서명되므로
새 빌드를 기존 설치 위에 그대로 덮어 설치할 수 있다 (DB 유지).

## 사용법

1. **게임 추가**: 게임 폴더(안에 `game/` 이 있는 폴더) 또는 `game/` 폴더 자체를 고른다.
   `Android/data` 아래는 SAF 로 접근할 수 없으니 게임을 다른 폴더(예: `내장 메모리/RenPy/`)에 둘 것.
2. **스캔**: `.rpa` 에서 스크립트만 꺼내고 `.rpy`/`.rpyc` 를 읽어 DB 에 반영한다.
   다시 스캔하면 신규/변경/삭제를 로그에 보여 준다 (게임 업데이트 후에 다시 스캔).
3. **번역 시작/이어하기**: Foreground Service 에서 배치 단위로 번역하고 배치마다 DB 에 저장한다.
   일시정지 후 다시 누르거나, 앱이 꺼진 뒤 다시 눌러도 남은 줄부터 이어 간다.
4. **출력 파일 생성**: `game/tl/<언어>/rpytl/` 에 번역 파일과 `_language.rpy`(언어 지정/폰트)를 쓴다.
   기본 폰트에 한글이 없으면 **폰트 선택**으로 한글 TTF/OTF 를 지정하고 다시 생성.
5. **검수필요**: 태그/변수(`[name]`, `{b}`, `%s` …) 복원에 실패한 줄. 해당 줄은 출력에서 빠지고
   (게임에서는 원문 표시) `검수필요 재번역`으로 다시 시도할 수 있다.

설정: 원문 언어, 번역 언어 코드, Google 요청 간격/묶음 크기, 실패 시 로컬 모델 전환,
로컬 모델 배치 크기/쓰레드 수(발열 조절), 모델 경로, 언어 강제 여부.

## PC 에서 엔진 테스트

```sh
cd engine
pip install pytest
python -m pytest -q
```

## 주의

- 게임 원본/저작물(`.rpa`, `.rpyc`, 추출한 스크립트, 번역 결과)은 커밋하지 않는다.
  테스트는 직접 만든 가짜 스크립트만 사용한다. `.gitignore` 에 확장자 차단이 걸려 있다.
- Google 번역은 비공식 `translate.googleapis.com` (gtx) 엔드포인트를 쓴다. 언제든 막힐 수 있다.
