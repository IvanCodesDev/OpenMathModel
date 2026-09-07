"""Reviewer 静态检查（static_checks.py）：规则正反例、白名单、种子、语法错误、材料与反馈。"""

from __future__ import annotations

import pytest

from omm_agent_skills import (
    CLEANING_STATIC_PROFILE,
    EXPERIMENT_STATIC_PROFILE,
    STATIC_RULES,
    VALIDATION_STATIC_PROFILE,
    StaticCheckProfile,
    major_static_feedback,
    run_static_checks,
    static_material,
)

CLEAN_EXPERIMENT = (
    "import json\n"
    "import random\n"
    "import numpy as np\n"
    "random.seed(42)\n"
    "np.random.seed(42)\n"
    "rows = [random.random() for _ in range(10)]\n"
    "with open('data/orders.csv', encoding='utf-8') as fh:\n"
    "    raw = fh.read()\n"
    "with open('results.csv', 'w', encoding='utf-8', newline='') as fh:\n"
    "    fh.write('metric,value\\nrmse,0.5\\n')\n"
    "print('OMM_METRICS_JSON: ' + json.dumps({'rmse': 0.5}))\n"
)


def ids(findings):
    return [item["id"] for item in findings]


def test_rule_table_has_fixed_severities():
    assert {rule: severity for rule, (severity, _) in STATIC_RULES.items()} == {
        "syntax_error": "major", "banned_call": "major", "network": "major", "no_seed": "major",
        "abs_path": "major", "read_outside_whitelist": "minor", "write_outside_whitelist": "minor",
        "unused_import": "minor", "fake_metrics_line": "minor",
    }


def test_clean_script_has_no_findings_and_material_says_so():
    findings = run_static_checks(CLEAN_EXPERIMENT, EXPERIMENT_STATIC_PROFILE)
    assert findings == []
    assert static_material(findings, EXPERIMENT_STATIC_PROFILE).startswith("静态检查（experiment）：0 项发现")
    assert major_static_feedback(findings) is None
    assert static_material(None) == "静态检查：未执行（本消费方未配置检查口径）。"


@pytest.mark.parametrize(
    ("code", "rule", "detail_part"),
    [
        ("import subprocess\nsubprocess.run(['ls'])\n", "banned_call", "subprocess"),
        ("import os\nos.system('rm -rf x')\n", "banned_call", "os.system(…)"),
        ("value = eval('1+1')\n", "banned_call", "eval(…)"),
        ("name = input('x')\n", "banned_call", "input(…)"),
        ("import shutil\nshutil.rmtree('cleaned')\n", "banned_call", "shutil.rmtree(…)"),
        ("import requests\nrequests.get('https://example.test')\n", "network", "requests"),
        ("import urllib.request\nurllib.request.urlopen('https://x')\n", "network", "urllib"),
        ("from socket import socket\ns = socket()\n", "network", "import socket"),
        ("import random\nx = random.random()\n", "no_seed", "random"),
        ("import numpy as np\nx = np.random.normal(size=3)\n", "no_seed", "random"),
        ("import numpy as np\nrng = np.random.default_rng()\n", "no_seed", "random"),
        ("import torch\nx = torch.rand(3)\n", "no_seed", "random"),
        ("with open('C:/Users/me/secret.csv') as fh:\n    pass\n", "abs_path", "读 C:/Users/me/secret.csv"),
        ("with open('/etc/passwd') as fh:\n    pass\n", "abs_path", "/etc/passwd"),
        ("import pandas as pd\ndf = pd.read_csv('../outside.csv')\n", "abs_path", "../outside.csv"),
        ("with open('data/../../x.csv') as fh:\n    pass\n", "abs_path", "data/../../x.csv"),
        ("def broken(:\n    pass\n", "syntax_error", ""),
    ],
)
def test_major_rules_fire(code, rule, detail_part):
    findings = run_static_checks(code, EXPERIMENT_STATIC_PROFILE)
    hits = [item for item in findings if item["id"] == rule]
    assert hits, findings
    assert hits[0]["severity"] == "major"
    assert detail_part in hits[0]["detail"]
    assert major_static_feedback(findings) is not None


@pytest.mark.parametrize(
    "code",
    [
        "import random\nrandom.seed(42)\nx = random.random()\n",
        "import numpy as np\nnp.random.seed(42)\nx = np.random.normal()\n",
        "import numpy as np\nrng = np.random.default_rng(42)\nx = rng.normal()\n",
        "from sklearn.model_selection import train_test_split\na, b = train_test_split([1, 2, 3], random_state=42)\n",
        "import torch\ntorch.manual_seed(42)\nx = torch.rand(3)\n",
        "import numpy as np\nx = np.linspace(0, 1, 5)\n",
    ],
)
def test_seeded_or_deterministic_scripts_do_not_trigger_no_seed(code):
    findings = run_static_checks(code, EXPERIMENT_STATIC_PROFILE)
    assert "no_seed" not in ids(findings), findings


def test_no_seed_is_not_required_when_profile_says_so():
    profile = StaticCheckProfile(name="free", require_seed=False)
    assert run_static_checks("import random\nx = random.random()\n", profile) == []


def test_read_write_whitelists_are_minor_and_per_consumer():
    code = (
        "import pandas as pd\n"
        "raw = pd.read_csv('data/orders.csv')\n"
        "extra = pd.read_csv('notes/extra.csv')\n"
        "raw.to_csv('cleaned/orders.csv', index=False)\n"
        "raw.to_csv('results.csv', index=False)\n"
        "with open('scratch/log.txt', 'w') as fh:\n"
        "    fh.write('x')\n"
    )
    cleaning = run_static_checks(code, CLEANING_STATIC_PROFILE)
    assert ids(cleaning) == ["read_outside_whitelist", "write_outside_whitelist", "write_outside_whitelist"]
    assert all(item["severity"] == "minor" for item in cleaning)
    assert cleaning[0]["detail"].startswith("读 notes/extra.csv（允许前缀：data/、cleaned/）")
    assert cleaning[1]["detail"].startswith("写 results.csv（允许前缀：cleaned/）")
    assert cleaning[2]["location"] == "第 6 行"
    assert major_static_feedback(cleaning) is None

    experiment = run_static_checks(code, EXPERIMENT_STATIC_PROFILE)
    assert ids(experiment) == ["read_outside_whitelist", "write_outside_whitelist", "write_outside_whitelist"]
    assert "写 cleaned/orders.csv" in experiment[1]["detail"] and "写 scratch/log.txt" in experiment[2]["detail"]

    # 检验阶段可读实验脚本正文（allowed_read_files），其余同实验
    validation = run_static_checks("code = open('experiment.py').read()\n", VALIDATION_STATIC_PROFILE)
    assert validation == []
    assert ids(run_static_checks("code = open('experiment.py').read()\n", EXPERIMENT_STATIC_PROFILE)) == ["read_outside_whitelist"]


def test_unused_import_and_fake_metrics_line_are_minor():
    code = (
        "import json\n"
        "import numpy as np\n"
        "from math import sqrt\n"
        "print('OMM_RESULT: done')\n"
        "print(f'OMM_STATUS {1}')\n"
        "print('OMM_METRICS_JSON: ' + json.dumps({'rmse': 0.5}))\n"
    )
    findings = run_static_checks(code, EXPERIMENT_STATIC_PROFILE)
    assert [(item["id"], item["location"]) for item in findings] == [
        ("unused_import", "第 2 行"),
        ("unused_import", "第 3 行"),
        ("fake_metrics_line", "第 4 行"),
        ("fake_metrics_line", "第 5 行"),
    ]
    assert findings[0]["detail"] == "import np" and findings[1]["detail"] == "import sqrt"
    assert findings[2]["detail"] == "OMM_RESULT: done"
    assert all(item["severity"] == "minor" for item in findings)


def test_findings_are_sorted_deduplicated_and_material_is_bounded():
    code = "import subprocess\nrunner = subprocess\n" + "".join(f"x{n} = open('/abs/{n}')\n" for n in range(14))
    findings = run_static_checks(code, EXPERIMENT_STATIC_PROFILE)
    assert ids(findings)[0] == "banned_call" and ids(findings).count("abs_path") == 14
    assert [item["location"] for item in findings][:3] == ["第 1 行", "第 3 行", "第 4 行"]
    material = static_material(findings, EXPERIMENT_STATIC_PROFILE)
    assert material.startswith("静态检查：15 项发现（阻断 15 项，提示 0 项）。")
    assert material.count("\n- [major]") == 12 and material.endswith("- …（共 15 项）")
    feedback = major_static_feedback(findings)
    assert feedback.startswith("静态检查有 15 项阻断性问题，必须修正后重新运行：")
    assert "- 第 1 行：调用了沙盒禁止的能力（子进程 / 动态执行 / 交互输入 / 删文件）（import subprocess）" in feedback
    assert feedback.endswith("修法：删除或替换被禁止的调用；随机性显式 seed（任务卡给的 random_seed）；文件只用工作区内的相对路径。其余逻辑不变。")


def test_syntax_error_reports_line_and_blocks():
    findings = run_static_checks("x = (\n", EXPERIMENT_STATIC_PROFILE)
    assert len(findings) == 1 and findings[0]["id"] == "syntax_error" and findings[0]["severity"] == "major"
    assert findings[0]["location"].startswith("第 ")
    assert "脚本无法解析" in static_material(findings)
