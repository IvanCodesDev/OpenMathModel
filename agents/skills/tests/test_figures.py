"""真实图件清单（figures.py）：终答说明解析、产物筛图、跨阶段编号、材料渲染、已插入判定。"""

from __future__ import annotations

from omm_agent_core.models import ArtifactRef
from omm_agent_skills import (
    available_figure_names,
    figure_inventory,
    figure_manifest,
    mark_inserted,
    parse_figure_notes,
    render_figure_material,
)


def ref(name, kind="figure", artifact_id="art_1", media_type="image/png"):
    return ArtifactRef(
        artifact_id=artifact_id,
        kind=kind,
        uri=f"C:/runs/run_1/artifacts/{artifact_id}/{name}",
        sha256="00",
        size=1,
        media_type=media_type,
        producer_step="step_1",
    )


# -- figure_notes 解析 -------------------------------------------------------------


def test_parse_figure_notes_accepts_common_separators_bullets_and_paths():
    notes = parse_figure_notes(
        "fit.png — 拟合曲线\n"
        "- `conv.svg`: 收敛曲线。\n"
        "* figures/residual.png - 残差分布\n"
        "sens.png： 灵敏度\n"
        "loss-curve.png 训练损失\n"
        "无\n"
        "这一行没有文件名\n"
    )
    assert notes == {
        "fit.png": "拟合曲线",
        "conv.svg": "收敛曲线",
        "residual.png": "残差分布",
        "sens.png": "灵敏度",
        "loss-curve.png": "训练损失",
    }


def test_parse_figure_notes_ignores_empty_none_and_caps_length():
    assert parse_figure_notes(None) == {}
    assert parse_figure_notes("") == {}
    assert parse_figure_notes("无") == {}
    long = parse_figure_notes("a.png — " + "长" * 300)
    assert len(long["a.png"]) == 200
    # 同名只取第一条
    assert parse_figure_notes("a.png — 第一\na.png — 第二") == {"a.png": "第一"}


# -- 本步产物 → 图件清单 -----------------------------------------------------------


def test_figure_manifest_keeps_only_figure_artifacts_and_intersects_notes():
    artifacts = [
        ref("results.csv", kind="table", artifact_id="art_t"),
        ref("fit.png", artifact_id="art_f1"),
        ref("conv.svg", artifact_id="art_f2", media_type="image/svg+xml"),
        ref("fit.png", artifact_id="art_f1_again"),  # 同名重复：只算一次
        ref("experiment.py", kind="code", artifact_id="art_c"),
    ]
    manifest = figure_manifest(artifacts, "fit.png — 拟合曲线\nghost.png — 没画出来的图")
    assert manifest == [
        {"name": "fit.png", "artifact_id": "art_f1", "media_type": "image/png", "caption": "拟合曲线"},
        {"name": "conv.svg", "artifact_id": "art_f2", "media_type": "image/svg+xml", "caption": ""},
    ]


def test_figure_manifest_without_figures_is_empty():
    assert figure_manifest([ref("results.csv", kind="table")], "fit.png — 说明") == []
    assert figure_manifest([], None) == []


# -- 跨阶段编号 ---------------------------------------------------------------------


def test_figure_inventory_numbers_experiment_then_validation_and_dedupes():
    prior = {
        "EXPERIMENTING": {
            "figures": [
                {"name": "fit.png", "artifact_id": "art_1", "caption": "拟合"},
                {"name": "conv.svg", "artifact_id": "art_2", "caption": ""},
                {"name": "", "artifact_id": "art_x"},  # 畸形：无名
                "not a mapping",
            ],
        },
        "VALIDATING": {
            "robustness": {
                "executed": True,
                "figures": [
                    {"name": "sens.png", "artifact_id": "art_3", "caption": "灵敏度"},
                    {"name": "fit.png", "artifact_id": "art_dup", "caption": "重复"},
                ],
            },
        },
    }
    assert figure_inventory(prior) == [
        {"number": 1, "name": "fit.png", "artifact_id": "art_1", "caption": "拟合", "source_stage": "EXPERIMENTING"},
        {"number": 2, "name": "conv.svg", "artifact_id": "art_2", "caption": "", "source_stage": "EXPERIMENTING"},
        {"number": 3, "name": "sens.png", "artifact_id": "art_3", "caption": "灵敏度", "source_stage": "VALIDATING"},
    ]


def test_figure_inventory_tolerates_missing_stages_and_shapes():
    assert figure_inventory({}) == []
    assert figure_inventory({"EXPERIMENTING": {"figures": "oops"}}) == []
    assert figure_inventory({"VALIDATING": {"robustness": {"executed": False}}}) == []
    # 只有检验图：从 1 编号
    only_validation = figure_inventory({
        "VALIDATING": {"robustness": {"figures": [{"name": "sens.png", "artifact_id": "a"}]}}
    })
    assert [(f["number"], f["source_stage"]) for f in only_validation] == [(1, "VALIDATING")]


# -- 材料与审计集合 ----------------------------------------------------------------


def test_render_figure_material_lists_numbered_rows_or_states_none():
    inventory = figure_inventory({
        "EXPERIMENTING": {"figures": [
            {"name": "fit.png", "artifact_id": "a", "caption": "拟合"},
            {"name": "conv.svg", "artifact_id": "b", "caption": ""},
        ]},
    })
    material = render_figure_material(inventory)
    assert "| 编号 | 文件名 | 来源 | 说明 |" in material
    assert "| 图 1 | fit.png | 实验阶段 | 拟合 |" in material
    assert "| 图 2 | conv.svg | 实验阶段 | （画图工程师未给说明，按文件名与实验摘要拟题） |" in material
    assert "`![图 N 标题](文件名)`" in material
    assert render_figure_material([]).startswith("无（本次运行没有产出图件")
    assert available_figure_names(inventory) == {"fit.png", "conv.svg"}
    assert available_figure_names([]) == set()


def test_mark_inserted_matches_markdown_and_html_images_by_url_or_basename():
    inventory = figure_inventory({
        "EXPERIMENTING": {"figures": [
            {"name": "fit.png", "artifact_id": "a"},
            {"name": "conv.svg", "artifact_id": "b"},
            {"name": "sens.png", "artifact_id": "c"},
        ]},
    })
    sections = [
        {"heading": "1", "content": "见图 1。\n\n![图 1 拟合](figures/fit.png)\n"},
        {"heading": "2", "content": '<img src="conv.svg" alt="图 2">'},
        {"heading": "3", "content": "只提了 sens.png 这个名字，没有插图。"},
    ]
    assert [(f["name"], f["inserted"]) for f in mark_inserted(inventory, sections)] == [
        ("fit.png", True),
        ("conv.svg", True),
        ("sens.png", False),
    ]
    # 原清单不被改写
    assert "inserted" not in inventory[0]
