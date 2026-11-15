# RPGCreator (`v0.1.0` · 실험 단계 테스트 버전)

**RPG 만들기(RPG Maker XP / MV / MZ / VX Ace) 프로젝트와 호환되는 비주얼 스튜디오 GUI · CLI 통합 제작 도구입니다.**

- **GitHub 저장소**: [L-Jack-00/RPGCreator](https://github.com/L-Jack-00/RPGCreator)
- **테스트 릴리즈 다운로드**: [Releases (v0.1.0)](https://github.com/L-Jack-00/RPGCreator/releases)
- **버그 제보 / 피드백**: [GitHub Issues](https://github.com/L-Jack-00/RPGCreator/issues)

> [!WARNING]
> **실험 단계(Experimental) 안내 — `v0.1.0` 테스트 버전**
>
> 본 버전(`v0.1.0`)은 핵심 편집·검증·실행 기능을 통합 검증하기 위한 **초기 실험용 테스트 릴리즈**입니다.
> - 일부 에디터 UI, 외부 엔진 변환(`import-mv`, `import-vxace`, `import-tiled`), 오디오/그래픽 렌더링, 패키징 옵션에 예상치 못한 오류나 호환성 제한이 발생할 수 있습니다.
> - 향후 정식 버전 과정에서 설정 구조, CLI 옵션 및 중간 작업 형식(`ir/`)이 변경될 수 있습니다.
> - **기존에 제작 중이던 소중한 프로젝트에 사용하기 전에는 반드시 원본 폴더를 별도로 백업**한 뒤 테스트해 주세요.

---

## 30초 빠른 시작 (Windows 단일 실행 파일)

별도 Python 설치나 명령어 입력 없이 바로 체험할 수 있습니다.

1. [GitHub Releases](https://github.com/L-Jack-00/RPGCreator/releases)에서 **`RPGCreator-v0.1.0-win64.zip`**을 내려받아 원하는 폴더에 압축 해제합니다.
2. 압축 해제한 폴더의 **`RPGCreatorStudio.exe`**를 더블클릭합니다. *(단일 파일 압축 해제로 인해 최초 실행 시 약 4~6초 정도 소요될 수 있습니다.)*
3. 기본 샘플 프로젝트(`./sample_workspace/ir`)가 자동으로 열리면 상단의 **`▶ 플레이테스트 (F6)`** 버튼을 눌러 바로 게임을 실행해 보세요.
   - 새 샘플 프로젝트를 만들려면 상단 **`✨ 새 샘플 생성`**, 다른 IR 프로젝트 폴더를 열려면 **`📂 IR 열기`** 또는 폴더를 `.exe` 위에 드래그 앤 드롭하세요.
4. 맵·타일·NPC 대사를 수정한 뒤 **`💾 저장`** → **`▶ 플레이테스트 (F6)`**으로 즉시 결과를 확인할 수 있습니다. 하단 콘솔에 `고블린 체력 200`, `밸런스 체크`, `도움말` 같은 한국어 명령을 입력해 바로 실행할 수도 있습니다.

---

## 주요 기능

1. **표준 RMXP 샘플 프로젝트 생성 (`sample`)**
   - Ruby 설치 없이 표준 `.rxdata` (`System`, `MapInfos`, `Tilesets`, `Actors`, `Classes`, `Skills`, `Items`, `Weapons`, `Armors`, `Enemies`, `Troops`, `States`, `Animations`, `CommonEvents`, `Map001~002`, `Scripts`), `Game.rxproj`, 32×32 PNG 타일셋·스프라이트 및 편집용 IR 워크스페이스(`ir/`) 생성
2. **양방향 프로젝트 변환 및 임포터 (`unpack` / `pack` / `unpack-rgssad` / `pack-rgssad` / `import-mv` / `import-tiled` / `import-vxace`)**
   - 순수 Python Ruby Marshal 4.8 파서 기반 `.rxdata` 양방향 읽기/쓰기 (`--encoding cp949/cp932` 레거시 인코딩 지원)
   - RGSSAD v1(`.rgssad`), v2(`.rgss2a`), v3(`.rgss3a`) 아카이브 추출 및 재패킹
   - RPG Maker MV/MZ JSON(`import-mv`), Tiled `.tmj`/`.tmx`/`.tsx`(`import-tiled`), RPG Maker VX Ace `.rvdata2`(`import-vxace`) 변환 지원
3. **비주얼 RPG 스튜디오 GUI (`RPGCreatorStudio.exe` / `studio`)**
   - **탭 1 — 맵 · 타일 · 이벤트**: 맵 생성/삭제, 크기·타일셋·BGM/BGS·패럴랙스 설정, 레이어 0/1/2 브러시·채우기·지우개·스포이드·시작 좌표 지정, 다중 페이지 이벤트 및 7종 간편 이벤트 빌더, `Ctrl+Z`/`Ctrl+Y` 실행 취소/다시 실행
   - **탭 2 — 데이터베이스 · 밸런스**: 액터·직업·스킬·아이템·무기·방어구·적·적 그룹·상태이상·타일셋 통행/우선순위·스위치·변수 편집 및 일괄 배율 밸런스 튜닝
   - **탭 3 — 커먼 이벤트 · 애니메이션**: 커먼 이벤트 CRUD, 애니메이션 셀/타이밍 편집 및 실시간 재생 미리보기
   - **탭 4 — 검증 · 테스트 · 다국어 · 패키징**: 맵 도달성/참조 무결성 검사, 리소스 누락 점검, 전투 승률 추정, Mermaid/DOT 흐름도, 맵 PNG 내보내기, 다국어 추출/적용, 배포 번들 생성
   - **탭 5 — 에셋 브라우저**: 프로젝트 내 `Graphics`, `Audio`, `Fonts` 목록 탐색, 확대/축소, 오디오 미리듣기, 외부 리소스 가져오기
   - **하단 콘솔 도크**: CLI 명령, JSON 블루프린트, 한국어/영문 자연어 명령 즉시 실행 및 실시간 편집 연동
4. **무결성 검증 · 자동 플레이 테스트 · 독립 실행 플레이어 (`check` / `check-assets` / `balance-check` / `sim` / `play` / `export-release`)**
   - 스위치/변수 조건을 추적하는 다중 맵 BFS 도달성 검증(`check`) 및 미사용/누락 에셋 검사(`check-assets`)
   - 스크립트 기반 자동 플레이·무작위 탐색 테스트(`sim`) 및 실시간 60 FPS Pygame 플레이 창(`play`, `F11` 전체화면, 세이브/로드 무결성 체크섬, BGM/BGS/SE/ME 믹서, 커스텀 키 바인딩)
   - `export-release`: 검증 후 리소스/폰트 정리, RGSSAD v1~v3 패킹, 런처/PyInstaller EXE 및 SHA-256 `manifest.json` 패키징

> [!NOTE]
> 본 릴리즈의 창 이동/크기 조절과 패널 구분선에는 드래그 중 멈춤을 줄이기 위한 **영역 미리보기 방식(회색 패턴 프레임 표시 후 배치 시 그래픽 갱신)**이 적용되어 있습니다.

---

## 소스 코드로 설치 및 실행 (Python 3.10+)

```bash
git clone https://github.com/L-Jack-00/RPGCreator.git
cd RPGCreator

pip install -r requirements.txt
# 선택: editable 설치 후 전역 `rpgcreator` 명령 사용 가능
pip install -e .
```

### 빠른 시작 (CLI)

```bash
# 1. 표준 RMXP 샘플 프로젝트 생성 (rmxp/ + ir/ 동시 생성)
python run_app.py sample ./sample_workspace

# 2. 비주얼 RPG 스튜디오 GUI 실행 (경로 생략 시 ./sample_workspace/ir 자동 로드)
python run_app.py studio ./sample_workspace/ir

# 3. 무결성·리소스·전투 밸런스 검사
python run_app.py inspect ./sample_workspace/ir
python run_app.py check ./sample_workspace/ir
python run_app.py check-assets ./sample_workspace/ir
python run_app.py balance-check ./sample_workspace/ir

# 4. 실시간 Pygame 플레이 창 실행 (타이틀 화면 시작, 영문 UI는 --lang en)
python run_app.py play ./sample_workspace/ir --title

# 5. 헤드리스 자동 플레이 및 무작위 탐색 테스트
python run_app.py sim ./sample_workspace/ir --actions right,up,interact --json
python run_app.py sim ./sample_workspace/ir --fuzz-steps 200 --seed 1 --json

# 6. 배포 번들 내보내기
python run_app.py export-release ./sample_workspace/ir ./dist_release --rgssad --rgssad-version 2 --launcher --zip --json

# 7. 전체 단위 테스트 실행
python -B -m unittest discover -s tests -p "test_*.py" -v
```

---

## CLI 레퍼런스

모든 명령은 `python run_app.py <subcommand> ...` 형태로 실행하며, `--json` 플래그를 서브커맨드 앞 또는 뒤에 붙여 구조화된 JSON 결과를 받을 수 있습니다.

<details>
<summary>전체 CLI 서브커맨드 목록 펼치기</summary>

### 프로젝트 생성 · 변환 · 아카이브 · 임포터
- `sample <output_dir>`
- `unpack <rmxp_dir> <ir_dir> [--encoding utf-8|cp949|cp932]`
- `pack <ir_dir> <rmxp_dir> [--encoding utf-8|cp949|cp932]`
- `unpack-rgssad <archive> <output_dir> [--key KEY]`
- `pack-rgssad <source_dir> <archive> [--key KEY] [--version 1|2|3]`
- `import-mv <src_dir> <out_ir_dir>` (RPG Maker MV/MZ)
- `import-tiled <tiled_file> <ir_dir> [--map-id ID] [--tileset-id ID]` (Tiled `.tmj`/`.tmx`/`.tsx`)
- `import-vxace <vxace_dir> <out_ir_dir>` (RPG Maker VX Ace `.rvdata2`)

### 맵 · 이벤트 · DB · 이력 · 다국어
- `map list <ir_dir>` / `map delete <ir_dir> --map-id ID`
- `map create <ir_dir> --name NAME [--width 20] [--height 15] [--tileset-id 1] [--fill-tile 384]`
- `map paint <ir_dir> --map-id ID --tile TILE_ID [--layer 0|1|2] [--rect x,y,w,h | --point=x,y]`
- `event list <ir_dir> --map-id ID` / `event delete <ir_dir> --map-id ID --event-id ID`
- `event add <ir_dir> --map-id ID --name NAME --x X --y Y [--trigger action_button|...] [--graphic NAME] [--through] [--say "대사1|대사2"] [--warp map_id,x,y] [--commands-json JSON]`
- `db get <ir_dir> --table TABLE --id ID` / `db delete <ir_dir> --table TABLE --id ID`
- `db set <ir_dir> --table TABLE --id ID --field k=v [--field k2=v2 ...]`
- `common-event list <ir_dir>` / `common-event set <ir_dir> --id ID --name NAME [--trigger none|autorun|parallel] [--switch-id ID] [--say ...] [--commands-json JSON]`
- `anim list <ir_dir>` / `anim set <ir_dir> --id ID --name NAME [--sheet SHEET] [--frames 6] [--se SE] [--cells-json JSON] [--timings-json JSON]`
- `balance-tune <ir_dir> [--enemy-hp-mult 1.0] [--enemy-atk-mult 1.0] [--exp-mult 1.0] [--gold-mult 1.0] [--skill-power-mult 1.0] [--dry-run]`
- `apply-blueprint <ir_dir> (--file PATH | --json-str JSON) [--no-check] [--dry-run]`
- `undo <ir_dir>` / `redo <ir_dir>`
- `i18n extract <ir_dir> [--out PATH] [--default-locale ko]` / `i18n apply <ir_dir> --lang LANG [--file PATH]`
- `workbench <ir_dir>`: 대화형 CLI 제작 콘솔

### 검증 · 시뮬레이션 · 실행 · 패키징
- `inspect <ir_dir>` / `check <ir_dir>` / `check-assets <ir_dir> [--graphics-dir DIR] [--audio-dir DIR]`
- `balance-check <ir_dir> [--troop-id ID] [--trials 20]`
- `export-graph <ir_dir> [--format mermaid|dot] [--out PATH]`
- `preview <ir_dir> [--map-id 1] [--png PATH] [--graphics-dir DIR] [--switch id=ON|OFF]`
- `sim <ir_dir> [--actions a,b,c | --script PATH | --fuzz-steps N --seed S]`
- `play <ir_dir> [--graphics-dir DIR] [--title] [--lang ko|en] [--user-save] [--strict-save]`
- `studio [<ir_dir>] [--graphics-dir DIR]` (별칭 `gui`, `editor`)
- `export-release <ir_dir> <out_dir> [--rgssad] [--rgssad-key KEY] [--rgssad-version 1|2|3] [--lang LANG] [--launcher] [--icon PATH] [--zip] [--build-exe] [--strict-assets] [--strict-save]`

</details>

---

## GUI 플레이어 단축키 (`play`)

- **이동 / 상호작용**: 방향키 / `WASD` 이동, `Shift` 제자리 방향 전환, `Ctrl` 대시, `Z`/`Enter`/`Space` 상호작용·선택, 마우스 좌클릭 UI 선택
- **메뉴 / 취소**: `ESC` / 마우스 우클릭 메뉴 허브 열기 및 취소
- **저장 / 불러오기**: `O`(저장), `L`(로드), `F6`/`F9`(빠른 저장·로드), `Delete`(슬롯 삭제), `F10`(자동 저장 불러오기)
- **화면 / 편의**: `F11` / `Alt+Enter`(전체화면), `F12`(`screenshots/` 캡처), `J`(퀘스트 일지), `F8`/`N`(미니맵 토글), `F10` 필드(음소거), `F5`(리로드)

---

## 로드맵 (v0.1.0 이후 예정 사항)

- 실제 대규모 상용 RMXP / MV / VX Ace 프로젝트 호환성 검증 확대
- 스튜디오 UI 세부 편의성 개선 및 릴리즈 후 피드백 반영
- 멀티 플랫폼(Windows/Linux/macOS) 패키징 테스트 및 안정화
