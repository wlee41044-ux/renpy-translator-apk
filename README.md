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
- [x] 4단계: 로컬 모델 (llama.cpp GGUF)

## 폰만으로 빌드/설치

1. 이 저장소에 push 하면 Actions 가 debug APK 를 빌드한다.
2. GitHub 앱/웹 → Actions → 최신 `build` 실행 → Artifacts 의 `rpytl-debug-apk` 다운로드
   (zip 안에 APK). 로그인 필요.
3. `v0.1.0` 같은 태그를 push 하면 Releases 에 APK 가 첨부된다 (로그인 없이 다운로드 가능).

APK 는 저장소의 고정 debug 키(`app/debug.keystore`, 비밀 아님)로 서명되므로
새 빌드를 기존 설치 위에 그대로 덮어 설치할 수 있다 (DB 유지).

## 작동 방식

폰에 **설치된 Ren'Py 게임 앱**을 찾아 APK 에서 바로 스크립트를 꺼내 번역하고,
번역 파일을 게임의 `Android/data/<패키지>/files/game/tl/<언어>/rpytl/` 에 넣는다.

- APK(`/data/app/.../base.apk`)는 다른 앱도 권한 없이 읽을 수 있다. Ren'Py 안드로이드 빌드는
  `game/` 을 `assets/x-game/` 에 넣고 경로마다 `x-` 를 붙인다 (Play 애셋 팩은 `assets/game/`).
- Ren'Py 안드로이드(7.4 이상)는 `Android/data/<패키지>/files/game/` 이 있으면 검색 경로 맨 앞에
  넣는다 → APK 를 고치거나 재설치하지 않고 번역이 적용되고 세이브도 그대로다.
- Android 11+ 에서는 일반 앱이 다른 앱의 `Android/data` 에 쓸 수 없어서 **Shizuku** 로 쓴다.

### 준비: Shizuku

1. Shizuku 앱 설치 (GitHub RikkaApps/Shizuku 릴리스 또는 Play 스토어).
2. 개발자 옵션 → 무선 디버깅 켜기 → Shizuku 앱에서 "무선 디버깅으로 시작" (재부팅할 때마다 다시).
3. 이 앱 상단의 Shizuku 상태에서 "권한 요청" → 허용.
4. 번역할 게임은 **한 번 실행**해서 `Android/data/<패키지>/files` 폴더가 생겨 있어야 한다.

### 사용법

1. **설치된 게임 찾기** → 목록에서 **추가**.
2. **스캔**: APK 의 `.rpa` 에서 스크립트만 꺼내고 `.rpyc`/`.rpy` 를 읽어 DB 에 반영.
   게임을 업데이트한 뒤 다시 스캔하면 신규/변경/삭제를 로그에 보여 준다.
3. **번역 시작/이어하기**: Foreground Service 에서 배치 단위로 번역, 배치마다 DB 저장.
   일시정지 후 다시 누르거나 앱이 꺼진 뒤 다시 눌러도 남은 줄부터 이어 간다.
4. **출력 파일 생성**: Shizuku 로 `Android/data/<패키지>/files/game/tl/<언어>/rpytl/` 에 기록.
   게임을 완전히 종료하고 다시 실행하면 적용된다. 한글이 네모로 나오면 **폰트 선택** 후 다시 생성.
5. **검수필요**: 태그/변수(`[name]`, `{b}`, `%s` …) 복원에 실패한 줄. 출력에서 빠지고(원문 표시)
   `검수필요 재번역`으로 다시 시도할 수 있다.

### 아이템 설명 / 스킬 / 상태창이 번역 안 될 때

이런 글은 대사가 아니라 게임의 파이썬 코드 안 데이터에 들어 있다. 스캔할 때 코드 속 문자열(아이템 이름과
설명, 스킬 이름, 상태창 라벨 등)도 번역 목록에 넣고(`old/new`), 게임이 그 글을 화면에 표시할 때
Ren'Py 가 정확히 같은 글이면 바꿔 준다. 설정의 "스캔: 코드 속 문자열도 번역"으로 켜고 끈다(기본 켜짐).
- 이미 번역한 게임은 **스캔만 다시** 하면 새로 찾은 글만 "신규"가 되어 번역 이어하기로 번역된다.
- 한계: 코드가 글을 조립해서(`"공격력 " + str(x) + " 증가"`) 만들거나 이미지에 구워진 글,
  게임 자체 번역 시스템을 거치는 글은 이 방법으로 안 바뀔 수 있다.

### 폰트 (한글이 네모/빈칸으로 보일 때)

게임의 화려한 폰트나 타이틀 화면 폰트에는 한글 글리프가 없어서 글자가 안 보일 수 있다.
출력할 때마다 한글 폰트를 `tl/<언어>/rpytl/font.ttf` 로 같이 넣고, 기본 방식은
**한글 글자만 그 폰트로 그리고 영문/숫자/기호는 게임 원래 폰트를 그대로 쓴다.**
- 폰트를 직접 고르지 않으면 앱에 포함된 **나눔고딕**(SIL OFL 1.1, `app/src/main/assets/fonts/`)을 쓴다.
- 설정에서 "한글만 한글 폰트로"를 끄면 게임의 주요 스타일 폰트를 통째로 교체하는 옛 방식으로 바뀐다.
- 한계: 글자가 이미지로 구워진 버튼/타이틀(이미지 안의 "START" 등)은 텍스트가 아니라서 번역되지 않는다.

### 로컬 모델 (인터넷 없이)

1. Qwen3 4B(또는 3B급) **Q4_K_M** GGUF 파일을 폰에 받아 둔다 (예: Hugging Face 에서).
2. 설정 열기 → **GGUF 모델 선택** → 앱 전용 폴더로 복사된다 (한 번, 몇 분).
3. 엔진을 **로컬 모델**로 바꾸고 번역 시작. 또는 Google 엔진에서 "실패 시 로컬 모델로 전환".
4. 발열이 심하면: 쓰레드 수(기본 4)·배치 크기(기본 4)를 줄이거나 "배치 사이 쉬는 시간"을 늘린다.
   메모리가 부족하면 컨텍스트 길이(기본 4096)를 줄인다.

llama.cpp 는 `third_party/llama.cpp` 서브모듈(태그 b11951 고정)을 CI 에서 함께 빌드한다.
arm64 전용, `armv8.2-a+dotprod+i8mm+fp16` 으로 컴파일하므로 2021년 이전 칩에서는 동작하지 않을 수 있다.

PC 판 게임 폴더(JoiPlay 등)는 **폴더로 추가**로도 쓸 수 있다 (SAF, 해당 폴더의 `game/tl/` 에 기록).

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
