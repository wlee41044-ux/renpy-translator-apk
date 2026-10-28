# 구조

```
renpy-translator-apk/
├─ app/                         Android 앱 (Kotlin + Compose 최소 UI + Chaquopy)
│  └─ src/main/java/com/wlee41044/rpytl/
│     ├─ App.kt                 Python.start()
│     ├─ MainActivity.kt        화면 (게임 목록 / 현황 / 버튼 / 로그)
│     ├─ saf/                   SAF 폴더 접근: 스크립트 복사, tl 파일 쓰기       (3단계)
│     ├─ service/               Foreground Service, 배치 번역 루프 호출          (3단계)
│     └─ llm/                   llama.cpp JNI 브리지                              (4단계)
├─ engine/                      순수 Python 엔진 (Chaquopy 소스 + PC pytest)
│  ├─ src/rpytl/
│  │  ├─ pipeline.py            Kotlin 이 부르는 진입점 (JSON 문자열 반환)
│  │  ├─ model.py               Item, Ren'Py 식 say 코드/식별자 계산
│  │  ├─ rpa.py                 RPA-2.0/3.0/3.2 아카이브 읽기 (스크립트만 추출)
│  │  ├─ rpyc.py                .rpyc 로드 (안전 언피클러 + 가짜 클래스) → 대사/메뉴/문자열
│  │  ├─ rpyparse.py            .rpy 텍스트 파서 + Ren'Py 식 번역 식별자 계산
│  │  ├─ strscan.py             코드 안의 _("...") 문자열
│  │  ├─ version.py             게임의 Ren'Py 버전 감지 (식별자 규칙이 8.6 에서 바뀜)
│  │  ├─ protect.py             태그·변수 → 플레이스홀더, 복원/검증
│  │  ├─ db.py                  SQLite 스키마/쿼리
│  │  ├─ scan.py                스캔 + diff (신규/변경/삭제 분류, 이력)
│  │  ├─ tlgen.py               game/tl/<lang>/*.rpy 생성 + 언어 초기화 스크립트
│  │  ├─ runner.py              번역 큐: 대기 항목 배치 처리, 중단/재개   (3단계)
│  │  └─ translators/           엔진 인터페이스: base / google / local   (3·4단계)
│  └─ tests/                    직접 만든 가짜 .rpy/.rpyc/.rpa 로만 테스트
└─ .github/workflows/build.yml  pytest → debug APK artifact, v* 태그면 Release 첨부
```

## 데이터 흐름

1. SAF 로 고른 게임 폴더에서 Kotlin 이 `game/` 아래 `.rpy/.rpyc` 를 앱 내부 작업 폴더로 복사하고,
   `.rpa` 는 파일 디스크립터로 Python 에 넘긴다 (스크립트 멤버만 꺼냄, 이미지 등은 안 읽음).
2. Python 이 스크립트 → 항목(대사/문자열) 목록 → DB 와 diff.
3. 번역 루프는 DB 의 `pending` 만 처리하고 배치마다 커밋 → 언제 끊겨도 이어하기 가능.
4. tl 파일을 앱 작업 폴더에 생성 → Kotlin 이 SAF 로 `game/tl/<lang>/` 에 기록.
   원본 스크립트/아카이브는 절대 수정하지 않는다.

## 식별자 정책

- `.rpyc` 가 있으면 AST 의 `Translate` 노드 식별자를 그대로 쓴다 (Ren'Py 가 계산한 값 = 가장 정확).
- `.rpy` 만 있으면 Ren'Py 알고리즘(라벨 + md5(code) 앞 8자리, 중복시 `_1`..)으로 계산.
- 같은 파일 이름의 `.rpy` 와 `.rpyc` 가 둘 다 있으면 `.rpy` 를 쓴다 (요구사항: 있으면 그대로 사용).

## 검증

- `.rpy` 식별자 계산은 Ren'Py 저장소의 샘플 게임(the_question, tutorial)과 그 tl 파일로 대조했다
  (커밋하지 않음): the_question 70/70, tutorial 803/803 (나머지는 원본에서 삭제된 파일·대사 없는
  `nvl clear` 블록), 문자열 155/155. 테스트에는 그중 몇 개 식별자 값만 상수로 넣었다.
- Ren'Py 8.6 부터 `config.tlid_only_considers_say=True` 가 기본 → `voice` 등이 식별자에서 빠진다.
  `renpy/vc_version.py` 로 버전을 감지하고, 스크립트가 직접 설정한 값이 있으면 그걸 따른다.
- Ren'Py 는 같은 `old` 문자열 번역이 두 번 있으면 시작 시 예외로 멈춘다 → 출력에서 중복 제거,
  게임에 이미 있는 tl/<언어> (.rpy 및 아카이브 안 .rpyc) 의 식별자/문자열은 제외.
- screen 의 `text "..."` 리터럴은 Ren'Py 추출기가 빼먹지만 런타임에 `translate_string` 을 거치므로
  번역 대상에 포함한다.
- `.rpyc` 언피클러는 renpy.* 를 전부 동작 없는 가짜 클래스로 만들고, 허용 목록 밖의 함수는 호출하지
  않는다 (악성 피클 테스트 포함). Ren'Py 8.4+ 의 cslots 피클/`_name` 필드도 처리.

## DB 스키마 (SQLite, 게임별 `game_id` 로 구분)

```sql
games(id PK, name, folder_uri UNIQUE, language DEFAULT 'korean',
      created_at, updated_at, last_scan_at)

-- 원문 해시 → 번역문 (번역 메모리). 같은 원문은 한 번만 번역.
texts(game_id, src_hash, source, translation, engine,      -- src_hash = sha1(원문); engine: google|local|manual
      status,                                              -- pending|done|review
      note, updated_at,
      PRIMARY KEY(game_id, src_hash))

-- 스크립트의 각 줄(항목). kind: dialogue|string
entries(id PK, game_id, kind, ident,                       -- 대사: translate 식별자, 문자열: 원문
        src_hash, speaker, file, line, label, seq,
        tmpl,                                              -- tl 블록 재구성용 JSON (voice 줄, 화자/속성, with 절)
        active,                                            -- 0 이면 '미사용'(삭제된 줄, 번역은 보존)
        first_scan, last_scan,
        UNIQUE(game_id, kind, ident))

-- 변경 이력: 원문이 바뀐 줄의 전/후 원문과 번역.
-- '변경' 판정: 같은 식별자인데 원문이 다름(id 절) 또는 같은 파일 안에서 이전/새 순서를 정렬했을 때
-- 같은 자리에 들어온 항목 (difflib 'replace' 구간).
history(id PK, game_id, kind, old_ident, new_ident, old_hash, new_hash,
        old_source, new_source, old_translation, changed_at)

scans(id PK, game_id, at, added, changed, removed, unchanged)
```

화면 표시 상태 = `entries.active=0` → 미사용, 아니면 `texts.status` (완료/대기/검수필요).
