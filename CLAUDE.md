# CLAUDE.md — RPGCreator 작업 가이드

이 저장소는 RPG Maker XP 호환 게임을 만드는 Python 도구입니다. 사용자가 "게임을 만들어줘 / 맵·NPC·적을 추가해줘" 같은 요청을 하면,
**IR 워크스페이스(`<project>/ir/`)를 `run_app.py` CLI로 편집**하는 것이 기본 작업 방식입니다.

## 실행 환경

- Python 3.10+ 와 `pip install -r requirements.txt` (pygame, Pillow) 필요.
- `run_app.py`는 시작 시 tkinter를 import 하므로 **헤드리스 명령도 tkinter가 있는 Python**이어야 합니다.
  `ModuleNotFoundError: No module named 'tkinter'`가 나면 tkinter가 포함된 인터프리터(예: Linux `python3-tk` 설치)를 사용하세요.
- 모든 명령: `python run_app.py <subcommand> ... --json` (`--json`은 서브커맨드 앞/뒤 어디든 가능). 결과 파싱을 위해 **항상 `--json`을 붙이세요.**

## 작업 대상 프로젝트

- 사용자가 경로를 지정하면 그 IR 폴더(`.../ir`)를 사용합니다.
- 지정이 없으면 새 프로젝트를 만들 것을 권장합니다: `python run_app.py sample ./my_game` → 편집 대상은 `./my_game/ir`.
- `sample_workspace/`는 저장소에 커밋된 기본 샘플입니다. 사용자가 명시적으로 요청할 때만 수정하세요.

## 기본 워크플로 (반드시 이 순서)

1. **파악**: `inspect <ir> --json`, `map list <ir> --json`, `event list <ir> --map-id N --json`, `db get <ir> --table T --id N --json`
2. **계획 → 미리보기**: 여러 요소를 한꺼번에 바꿀 때는 Blueprint JSON 파일을 작성하고 먼저
   `apply-blueprint <ir> --file bp.json --dry-run --json` 으로 diff/검증 결과를 확인
3. **적용**: `apply-blueprint <ir> --file bp.json --json` (원자적 적용 + 자동 검증 + undo 기록)
4. **검증**: `check <ir> --json` (참조 무결성 + 맵 간 BFS 도달성). 필요 시 `check-assets`, `balance-check --troop-id N`, `sim --fuzz-steps 200 --seed 1`
5. **시각 확인**: `preview <ir> --map-id N` (ASCII 맵: `#`=막힘, `E`=이벤트, `:`=길, `P`=시작점)
6. 잘못됐으면 `undo <ir>` / `redo <ir>`

규칙:
- `maps/*.json`, `database/*.json`을 **직접 편집하지 말고** CLI/Blueprint를 사용하세요 (검증·undo 이력이 유지됨).
- `play`, `studio`는 GUI 창을 띄우고 종료되지 않으므로 직접 실행하지 말고, 사용자에게 실행 명령만 안내하세요:
  `python run_app.py play <ir> --title`
- 작업이 끝나면 `check` 결과(passed/errors/warnings)를 사용자에게 요약 보고하세요.

## 자주 쓰는 단일 명령

```bash
python run_app.py map create <ir> --name "동굴" --width 20 --height 15 --fill-tile 384 --json
python run_app.py map paint <ir> --map-id 3 --layer 1 --tile 386 --rect 0,0,20,1 --json
python run_app.py event add <ir> --map-id 1 --name "상인" --x 5 --y 6 --graphic elder --say "어서 오세요!|좋은 물건 있어요." --json
python run_app.py event add <ir> --map-id 1 --name "동굴 입구" --x 1 --y 10 --trigger player_touch --warp 3,2,7 --json
python run_app.py db set <ir> --table enemies --id 1 --field maxhp=200 --field exp=30 --json
python run_app.py balance-tune <ir> --enemy-hp-mult 1.2 --exp-mult 1.1 --dry-run --json
```

## Blueprint JSON 스키마 요약

최상위 키(모두 선택): `db`, `maps`(새 맵), `patch_maps`(기존 맵 수정), `common_events`, `start`.
이름 참조(`map_name`, `switch_name`, `troop_name`, `item_name` 등)를 쓰면 ID를 몰라도 됩니다. 같은 Blueprint에서 새로 정의한 이름도 참조 가능합니다.

- `db`: `[{"table": T, "id": N, "fields": {...}}]`
  - 테이블: `actors, classes, skills, items, weapons, armors, enemies, troops, states, tilesets, switches, variables`
  - `switches`/`variables`는 `{"name": "..."}`만 가짐
  - `troops`는 `"enemy_names": ["고블린", ...]` 또는 `"enemy_ids": [1, 2]`로 멤버 지정
- `maps`: `{"name", "width", "height", "tileset_id", "fill_tile", "paint": [...], "events": [...]}`
- `patch_maps`: `{"map_id", "name"?, "paint"?, "events"?(추가), "patch_events"?, "remove_events"?}`
  - `patch_events`: `{"id" 또는 "name", "new_name"?, "x"?, "y"?, "trigger"?, "graphic"?, "commands"? | "pages"?}`
  - `remove_events`: `[{"id": 3}]` 또는 `[{"name": "상인"}]`
- `paint`: `{"layer": 0|1|2, "tile_id": N, "rect": [x, y, w, h]}` 또는 `"points": [[x, y], ...]`
- `events`: `{"name", "x", "y", "trigger"?, "graphic"?, "through"?, "commands": [...]}` 또는 다중 페이지 `"pages": [...]`
  - page: `{"trigger", "graphic", "condition"?: {"switch1_name"|"switch1_id", "variable_name", "variable_value", "self_switch_ch"}, "commands": [...]}`
  - 뒤 페이지일수록 우선순위가 높습니다 (조건이 충족되면 마지막 페이지가 표시됨)
- `start`: `{"map_id" | "map_name", "x", "y"}` (플레이어 시작 위치)

트리거: `action_button`(조사), `player_touch`(밟으면), `event_touch`, `autorun`, `parallel`

### 샘플 타일셋 타일 ID (tileset 1/2)

| ID | 의미 | 통행 |
|---|---|---|
| 384 | 풀밭 / 돌바닥 (기본 채우기) | O |
| 385 | 흙길 / 포장 바닥 | O |
| 386 | 나무 / 동굴 벽 (보통 layer 1에 칠함) | X |
| 387 | 나무 다리 | O |
| 388 | 동굴 / 출구 포털 | O |
| 389 | 꽃 / 크리스탈 장식 | O |

캐릭터 그래픽(샘플): `hero`, `elder`, `goblin`, `chest`

### 주요 이벤트 명령 (`commands`의 각 항목, `indent`는 중첩 깊이)

```json
{"op": "say", "lines": ["줄1", "줄2"], "indent": 0}
{"op": "show_choices", "choices": ["예", "아니오"], "cancel_type": 1, "indent": 0}
{"op": "when_choice", "index": 0, "label": "예", "indent": 0}
{"op": "end_choices", "indent": 0}
{"op": "if_switch", "switch_name": "보스_처치", "expected": "ON", "indent": 0}
{"op": "else", "indent": 0}
{"op": "end_if", "indent": 0}
{"op": "if_item", "item_name": "열쇠", "indent": 0}
{"op": "set_switch", "switch_name": "보스_처치", "value": "ON", "indent": 0}
{"op": "set_self_switch", "channel": "A", "value": "ON", "indent": 0}
{"op": "set_variable", "variable_name": "점수", "operation": 0, "operand_type": 0, "operand": [10], "indent": 0}
{"op": "change_item", "item_name": "회복약", "mode": "add", "operand_type": 0, "amount": 2, "indent": 0}
{"op": "change_gold", "mode": "add", "amount": 100, "indent": 0}
{"op": "transfer_player", "map_name": "깊은 숲", "x": 2, "y": 7, "direction": "right", "indent": 0}
{"op": "battle", "troop_name": "고블린 킹전", "can_escape": false, "indent": 0}
{"op": "shop", "goods": [{"item_name": "회복약"}, {"weapon_name": "철검"}], "indent": 0}
{"op": "recover_all", "actor_id": 0, "indent": 0}
{"op": "common_event", "common_event_name": "회복", "indent": 0}
```

분기 안의 명령은 `indent`를 1 늘리고, `else`/`end_if`/`when_choice`/`end_choices`는 분기 시작과 같은 indent를 씁니다.
이름 참조(`*_name`)는 해당 DB 항목/스위치/커먼 이벤트가 이미 있거나 같은 Blueprint에서 정의되어야 합니다.
그 외 명령(화면 효과, 그림, 이동 루트 등)은 `src/core/ir_converter.py`의 `semantic_dsl_to_commands` 처리부를 참고하세요.

### 검증된 예시: 새 숲 맵 + 마을 연결 + 보스전

```json
{
  "db": [
    {"table": "switches", "id": 10, "fields": {"name": "숲_보스_처치"}},
    {"table": "enemies", "id": 10, "fields": {"name": "고블린 킹", "maxhp": 400, "atk": 30, "exp": 120, "gold": 200}},
    {"table": "troops", "id": 10, "fields": {"name": "고블린 킹전", "enemy_names": ["고블린 킹"]}}
  ],
  "maps": [
    {
      "name": "깊은 숲", "width": 20, "height": 15, "tileset_id": 1, "fill_tile": 384,
      "paint": [
        {"layer": 1, "tile_id": 386, "rect": [0, 0, 20, 1]},
        {"layer": 1, "tile_id": 386, "rect": [0, 14, 20, 1]},
        {"layer": 0, "tile_id": 385, "rect": [2, 7, 16, 1]}
      ],
      "events": [
        {"name": "마을로", "x": 1, "y": 7, "trigger": "player_touch",
         "commands": [{"op": "transfer_player", "map_id": 1, "x": 2, "y": 10, "direction": "right", "indent": 0}]},
        {"name": "고블린 킹", "x": 16, "y": 7, "pages": [
          {"trigger": "action_button", "graphic": "goblin", "commands": [
            {"op": "say", "lines": ["고블린 킹: 감히 내 숲에 들어오다니!"], "indent": 0},
            {"op": "battle", "troop_name": "고블린 킹전", "can_escape": false, "indent": 0},
            {"op": "set_switch", "switch_name": "숲_보스_처치", "value": "ON", "indent": 0}
          ]},
          {"trigger": "action_button", "graphic": "", "condition": {"switch1_name": "숲_보스_처치"}, "commands": [
            {"op": "say", "lines": ["조용한 숲이다."], "indent": 0}
          ]}
        ]}
      ]
    }
  ],
  "patch_maps": [
    {"map_id": 1, "events": [
      {"name": "숲 입구", "x": 1, "y": 10, "trigger": "player_touch",
       "commands": [{"op": "transfer_player", "map_name": "깊은 숲", "x": 2, "y": 7, "direction": "right", "indent": 0}]}
    ]}
  ]
}
```

주의: 워프 도착 좌표와 이벤트 좌표는 맵 범위 안이어야 하고, 도착 지점이 벽(386)이면 `check`에서 도달 불가로 잡힙니다.
적용 후 `balance-check --troop-id 10`으로 난이도를 확인하고 `db set`으로 수치를 조정하세요.

## 이 도구 자체를 개발할 때

- 구조: `run_app.py`(CLI 진입점), `src/core/`(IR 변환·편집·검증·시뮬레이터), `src/harness/`(Pygame 플레이어, Tkinter 스튜디오, 에이전트 브리지)
- 테스트: `python -B -m unittest discover -s tests -p "test_*.py" -v` (GUI 테스트는 디스플레이 필요, Linux 헤드리스는 `xvfb-run -a` 사용)
