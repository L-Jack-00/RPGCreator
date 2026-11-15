# -*- coding: utf-8 -*-
"""
Core Engine (Decoupled from I/O)
Orchestrates RMXP sample generation, IR conversion, RGSSAD archiving,
atomic IR editing, headless simulation, and project integrity validation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .editor import (
    add_event,
    apply_blueprint,
    batch_tune_balance,
    create_map,
    delete_db_entry,
    delete_event,
    delete_map,
    export_release_bundle,
    get_db_entry,
    inspect_ir_project,
    list_animations,
    list_common_events,
    list_events,
    list_maps,
    paint_map,
    redo_last_edit,
    run_interactive_workbench,
    save_event,
    set_animation,
    set_common_event,
    set_db_entry,
    undo_last_edit,
    update_map_properties,
)
from .i18n import apply_i18n_locale, extract_i18n_strings
from .importers import import_rpgmaker_mv_mz, import_tiled_map, import_vxace_rxdata
from .ir_converter import pack_ir_to_rmxp, unpack_rmxp_to_ir
from .models import HarnessState, JobContext
from .rgssad import DEFAULT_RGSSAD_KEY, pack_rgssad, unpack_rgssad
from .sample_generator import generate_sample_project
from .simulator import run_fuzz_playtest, run_headless_simulation
from .validator import (
    ValidationResult,
    export_flow_graph,
    render_map_preview,
    simulate_battle_balance,
    validate_ir_project,
    validate_project_assets,
)


class CoreEngine:
    """Pure computation engine executing project conversion, editing, and verification tasks."""

    def __init__(self, on_progress: Optional[Callable[[float], None]] = None):
        self.on_progress = on_progress
        self.context: Optional[JobContext] = None

    def execute_step(self, context: JobContext, step_index: int, total_steps: int) -> bool:
        """Executes a single deterministic pipeline step."""
        self.context = context
        context.status = HarnessState.RUNNING

        tasks: Optional[List[Callable[[], Any]]] = context.payload.get("tasks")
        if tasks and step_index < len(tasks):
            result = tasks[step_index]()
            context.payload.setdefault("results", []).append(result)

        ratio = (step_index + 1) / max(total_steps, 1)
        context.progress_pct = round(ratio * 100.0, 2)

        if self.on_progress:
            self.on_progress(context.progress_pct)

        return True

    @staticmethod
    def create_sample(output_dir: Path) -> Dict[str, Any]:
        return generate_sample_project(output_dir)

    @staticmethod
    def unpack(rmxp_dir: Path, ir_dir: Path, encoding: str = "utf-8") -> Dict[str, Any]:
        return unpack_rmxp_to_ir(rmxp_dir, ir_dir, encoding=encoding)

    @staticmethod
    def pack(ir_dir: Path, rmxp_dir: Path, encoding: str = "utf-8") -> Dict[str, Any]:
        return pack_ir_to_rmxp(ir_dir, rmxp_dir, encoding=encoding)

    @staticmethod
    def unpack_archive(archive_path: Path, output_dir: Path, key: int = DEFAULT_RGSSAD_KEY) -> Dict[str, Any]:
        return unpack_rgssad(archive_path, output_dir, initial_key=key)

    @staticmethod
    def pack_archive(source_dir: Path, archive_path: Path, key: int = DEFAULT_RGSSAD_KEY, version: int = 1) -> Dict[str, Any]:
        return pack_rgssad(source_dir, archive_path, initial_key=key, version=version)

    @staticmethod
    def map_create(
        ir_dir: Path, name: str, width: int = 20, height: int = 15, tileset_id: int = 1, fill_tile: int = 384
    ) -> Dict[str, Any]:
        return create_map(ir_dir, name=name, width=width, height=height, tileset_id=tileset_id, fill_tile=fill_tile)

    @staticmethod
    def map_list(ir_dir: Path) -> List[Dict[str, Any]]:
        return list_maps(ir_dir)

    @staticmethod
    def map_delete(ir_dir: Path, map_id: int) -> Dict[str, Any]:
        return delete_map(ir_dir, map_id=map_id)

    @staticmethod
    def map_paint(
        ir_dir: Path,
        map_id: int,
        layer: int,
        tile_id: int,
        rect: Optional[Tuple[int, int, int, int]] = None,
        points: Optional[List[Tuple[int, int]]] = None,
    ) -> Dict[str, Any]:
        return paint_map(ir_dir, map_id=map_id, layer=layer, tile_id=tile_id, rect=rect, points=points)

    @staticmethod
    def event_add(
        ir_dir: Path,
        map_id: int,
        name: str,
        x: int,
        y: int,
        trigger: str = "action_button",
        graphic: str = "",
        through: bool = False,
        commands: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        return add_event(
            ir_dir, map_id=map_id, name=name, x=x, y=y, trigger=trigger, graphic=graphic, through=through, commands=commands
        )

    @staticmethod
    def event_list(ir_dir: Path, map_id: int) -> List[Dict[str, Any]]:
        return list_events(ir_dir, map_id=map_id)

    @staticmethod
    def event_delete(ir_dir: Path, map_id: int, event_id: int) -> Dict[str, Any]:
        return delete_event(ir_dir, map_id=map_id, event_id=event_id)

    @staticmethod
    def db_get(ir_dir: Path, table: str, entry_id: int) -> Dict[str, Any]:
        return get_db_entry(ir_dir, table=table, entry_id=entry_id)

    @staticmethod
    def db_set(ir_dir: Path, table: str, entry_id: int, fields: Dict[str, str]) -> Dict[str, Any]:
        return set_db_entry(ir_dir, table=table, entry_id=entry_id, fields=fields)

    @staticmethod
    def db_delete(ir_dir: Path, table: str, entry_id: int) -> Dict[str, Any]:
        return delete_db_entry(ir_dir, table=table, entry_id=entry_id)

    @staticmethod
    def map_update(ir_dir: Path, map_id: int, props: Dict[str, Any]) -> Dict[str, Any]:
        return update_map_properties(ir_dir, map_id=map_id, props=props)

    @staticmethod
    def event_save(ir_dir: Path, map_id: int, event_data: Dict[str, Any]) -> Dict[str, Any]:
        return save_event(ir_dir, map_id=map_id, event_data=event_data)

    @staticmethod
    def common_event_set(
        ir_dir: Path,
        entry_id: int,
        name: str,
        trigger: str | int = "none",
        switch_id: int = 1,
        commands: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        return set_common_event(
            ir_dir,
            entry_id=entry_id,
            name=name,
            trigger=trigger,
            switch_id=switch_id,
            commands=commands,
        )

    @staticmethod
    def common_event_list(ir_dir: Path) -> List[Dict[str, Any]]:
        return list_common_events(ir_dir)

    @staticmethod
    def blueprint_apply(
        ir_dir: Path,
        blueprint: Dict[str, Any],
        validate: bool = True,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        return apply_blueprint(ir_dir, blueprint=blueprint, validate=validate, dry_run=dry_run)

    @staticmethod
    def undo(ir_dir: Path) -> Dict[str, Any]:
        return undo_last_edit(ir_dir)

    @staticmethod
    def redo(ir_dir: Path) -> Dict[str, Any]:
        return redo_last_edit(ir_dir)

    @staticmethod
    def check(ir_dir: Path) -> ValidationResult:
        return validate_ir_project(ir_dir)

    @staticmethod
    def check_assets(
        ir_dir: Path,
        graphics_dir: Optional[Path] = None,
        audio_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        return validate_project_assets(ir_dir, graphics_dir=graphics_dir, audio_dir=audio_dir)

    @staticmethod
    def balance_check(
        ir_dir: Path,
        troop_id: Optional[int] = None,
        trials: int = 20,
    ) -> Dict[str, Any]:
        return simulate_battle_balance(ir_dir, troop_id=troop_id, trials_per_troop=trials)

    @staticmethod
    def export_graph(ir_dir: Path, fmt: str = "mermaid") -> str:
        return export_flow_graph(ir_dir, fmt=fmt)

    @staticmethod
    def inspect(ir_dir: Path) -> Dict[str, Any]:
        return inspect_ir_project(ir_dir)

    @staticmethod
    def preview(
        ir_dir: Path,
        map_id: int,
        png_out: Optional[Path] = None,
        graphics_dir: Optional[Path] = None,
        switches_state: Optional[Dict[int, bool]] = None,
    ) -> str:
        return render_map_preview(
            ir_dir,
            map_id=map_id,
            png_out=png_out,
            graphics_dir=graphics_dir,
            switches_state=switches_state,
        )

    @staticmethod
    def simulate(ir_dir: Path, actions: List[str]) -> Dict[str, Any]:
        return run_headless_simulation(ir_dir, actions=actions)

    @staticmethod
    def simulate_fuzz(ir_dir: Path, steps: int = 200, seed: int = 1) -> Dict[str, Any]:
        return run_fuzz_playtest(ir_dir, steps=steps, seed=seed)

    @staticmethod
    def i18n_extract(ir_dir: Path, out_path: Optional[Path] = None, default_locale: str = "ko") -> Dict[str, Any]:
        return extract_i18n_strings(ir_dir, out_path=out_path, default_locale=default_locale)

    @staticmethod
    def i18n_apply(ir_dir: Path, locale: str, strings_path: Optional[Path] = None) -> Dict[str, Any]:
        return apply_i18n_locale(ir_dir, locale=locale, strings_path=strings_path)

    @staticmethod
    def import_mv(src_dir: Path, out_ir_dir: Path) -> Dict[str, Any]:
        return import_rpgmaker_mv_mz(src_dir, out_ir_dir)

    @staticmethod
    def import_tiled(tiled_path: Path, ir_dir: Path, map_id: Optional[int] = None, tileset_id: int = 1) -> Dict[str, Any]:
        return import_tiled_map(tiled_path, ir_dir, map_id=map_id, tileset_id=tileset_id)

    @staticmethod
    def import_vxace(vxace_dir: Path, out_ir_dir: Path) -> Dict[str, Any]:
        return import_vxace_rxdata(vxace_dir, out_ir_dir)

    @staticmethod
    def list_animations(ir_dir: Path) -> List[Dict[str, Any]]:
        return list_animations(ir_dir)

    @staticmethod
    def set_animation(
        ir_dir: Path,
        anim_id: int,
        name: str,
        animation_name: str = "",
        position: int = 1,
        frame_max: int = 6,
        pattern_start: int = 0,
        zoom: int = 100,
        se_name: str = "",
        flash_color: Optional[List[int]] = None,
        cells: Optional[List[Dict[str, Any]]] = None,
        timings: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        return set_animation(
            ir_dir,
            anim_id=anim_id,
            name=name,
            animation_name=animation_name,
            position=position,
            frame_max=frame_max,
            pattern_start=pattern_start,
            zoom=zoom,
            se_name=se_name,
            flash_color=flash_color,
            cells=cells,
            timings=timings,
        )

    @staticmethod
    def batch_tune_balance(
        ir_dir: Path,
        enemy_hp_mult: float = 1.0,
        enemy_atk_mult: float = 1.0,
        exp_mult: float = 1.0,
        gold_mult: float = 1.0,
        skill_power_mult: float = 1.0,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        return batch_tune_balance(
            ir_dir,
            enemy_hp_mult=enemy_hp_mult,
            enemy_atk_mult=enemy_atk_mult,
            exp_mult=exp_mult,
            gold_mult=gold_mult,
            skill_power_mult=skill_power_mult,
            dry_run=dry_run,
        )

    @staticmethod
    def workbench(ir_dir: Path, stdin_lines: Optional[List[str]] = None) -> Dict[str, Any]:
        return run_interactive_workbench(ir_dir, stdin_lines=stdin_lines)

    @staticmethod
    def export_release(
        ir_dir: Path,
        out_dir: Path,
        use_rgssad: bool = False,
        lang: Optional[str] = None,
        launcher: bool = False,
        build_exe: bool = False,
        strict_assets: bool = False,
        rgssad_key: Optional[int] = None,
        rgssad_version: int = 3,
        icon_path: Optional[Path] = None,
        create_zip: bool = False,
        strict_save: bool = False,
    ) -> Dict[str, Any]:
        return export_release_bundle(
            ir_dir,
            out_dir=out_dir,
            use_rgssad=use_rgssad,
            lang=lang,
            launcher=launcher,
            build_exe=build_exe,
            strict_assets=strict_assets,
            rgssad_key=rgssad_key,
            rgssad_version=rgssad_version,
            icon_path=icon_path,
            create_zip=create_zip,
            strict_save=strict_save,
        )
