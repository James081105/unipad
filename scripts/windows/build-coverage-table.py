#!/usr/bin/env python3
"""Render the requirement -> implementation -> verification -> evidence table.

Why a generator and not a document
----------------------------------
The goal document (§18 step 1) asks for a real coverage table, and the honest
source for one already exists: ``python -m unipad.acceptance --run <id>`` writes
a bundle in which every registered check carries its PRD citation, its four
fields, its verdict, its evidence paths and its blocker.  Hand-writing a table
from that is how a table starts lying -- it is edited when someone remembers to,
and silently rots when a check changes.  So this script runs the gate and prints
the table, and ``docs/coverage-matrix.md`` is its output rather than its input.

What the table deliberately keeps apart
--------------------------------------
The four columns that CLAUDE.md forbids conflating: implementation state,
Windows/simulation state, runner verification, and on-site (real Windows
acceptance) state.  A cell reading ``PASS`` under one of them says nothing about
the others, which is the entire point of showing them side by side.

Privacy (PRD 9.2, PRD 12)
-------------------------
The bundle lives under ``local-private/`` and is never committed.  This script
prints only repo-relative evidence paths and a small set of host fields, and it
*refuses to print at all* if the current user's profile path or account name
appears anywhere in the output -- a guard, not a hope, because the last two
leaks in this repo (``host/inventory.py`` and ``controller/cli.py:816``) were
both an absolute path written somewhere nobody thought to look.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from unipad.acceptance import registry  # noqa: E402
from unipad.acceptance.runner import AcceptanceRunner  # noqa: E402

BUNDLE_DIRNAME = "coverage-matrix"


# ---------------------------------------------------------------------------
# privacy guard
# ---------------------------------------------------------------------------
def _forbidden_fragments() -> list[str]:
    """Strings that must never appear in the rendered table (PRD 9.2)."""
    fragments: list[str] = []
    for candidate in (os.environ.get("USERNAME"), os.environ.get("USERPROFILE")):
        if candidate and len(candidate) > 2:
            fragments.append(candidate)
    try:
        home = Path.home()
        if home.name and len(home.name) > 2:
            fragments.append(home.name)
        fragments.append(str(home))
    except (OSError, RuntimeError):  # pragma: no cover - no home directory
        pass
    return [fragment for fragment in fragments if fragment]


def _assert_no_user_data(text: str, fragments: list[str]) -> None:
    for fragment in fragments:
        if fragment and fragment in text:
            raise SystemExit(
                "refusing to print: the rendered table contains the current "
                f"user's identity ({fragment[:3]}...), which PRD 9.2 forbids in "
                "any evidence output.  Fix the field that leaked it."
            )


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def _cell(value: object, limit: int = 160) -> str:
    """One Markdown table cell: no pipes, no newlines, bounded length."""
    if isinstance(value, (list, tuple, set)):
        # A Python repr in a table cell is a table nobody can read, and the
        # aggregate is full of lists.
        value = ", ".join(_cell(item, limit) for item in value) or "—"
    elif isinstance(value, dict):
        # ``field_unverified`` entries are dicts; the two fields that matter are
        # which check is unproven and why.
        detail = value.get("blocker") or value.get("reason") or ""
        label = value.get("check_id") or value.get("field") or ""
        value = f"{label}: {detail}" if label else detail
    text = " ".join(str(value if value is not None else "").split())
    text = text.replace("|", "\\|")
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or "—"


def _evidence_paths(verdict: dict) -> str:
    """The repo-relative evidence paths, de-duplicated and bounded."""
    paths: list[str] = []
    for ref in verdict.get("evidence") or ():
        path = str(ref.get("path") or "").strip()
        if path and path not in paths:
            paths.append(path)
    if not paths:
        return "—"
    shown = ", ".join(f"`{path}`" for path in paths[:4])
    if len(paths) > 4:
        shown += f" (+{len(paths) - 4} more)"
    return shown


def _reasons(entry: dict, verdict: object) -> tuple[str, ...]:
    """Why a case reads the way it does, from whichever field carries it.

    A case's ``verdict`` is a word and its ``reasons`` sit next to it; a check's
    reasons sit inside its verdict object.  Reading only one of the two would
    print an empty column for the other.
    """
    if isinstance(verdict, dict):
        found = verdict.get("reasons")
    else:
        found = entry.get("reasons")
    if isinstance(found, str):
        return (found,)
    return tuple(str(item) for item in (found or ()))


def _verdict_kind(verdict: object) -> str:
    """The verdict's kind, whether the bundle spells it as an object or a word.

    ``checks`` carry a full ``Verdict`` object; ``cases`` carry the plain word,
    because a case verdict is a composite of its checks and has no evidence of
    its own to attach.  Both are read the same way here so the two tables stay
    comparable.
    """
    if isinstance(verdict, dict):
        return str(verdict.get("kind") or "—")
    if isinstance(verdict, str) and verdict.strip():
        return verdict.strip()
    return "—"


def _fields(entry: dict) -> dict:
    return entry.get("four_fields") or {}


def render(bundle: dict, run_id: str) -> str:
    checks = bundle.get("checks") or []
    cases = bundle.get("cases") or []
    aggregate = bundle.get("aggregate") or {}
    scope = bundle.get("scope_summary") or {}
    environment = (checks[0].get("environment") if checks else {}) or {}

    lines: list[str] = []
    lines.append("# UniPad 覆盖矩阵（需求 → 实现 → 验证 → 证据）")
    lines.append("")
    lines.append(
        "本文件由 `scripts/windows/build-coverage-table.py` 生成，请勿手改；"
        "改代码后重新生成。数据来源是一次真实的 `unipad.acceptance` 运行，"
        "不是对进度的转述。"
    )
    lines.append("")
    lines.append(f"- 运行 id：`{run_id}`")
    lines.append(f"- 写入时间：{bundle.get('written_at', '—')}")
    lines.append(f"- 门禁身份：`{bundle.get('verifier', {}).get('id', '—')}` "
                 f"v{bundle.get('verifier', {}).get('version', '—')}")
    lines.append(f"- 主机：{environment.get('host_label', '—')}")
    lines.append(f"- 加速器：{environment.get('accel', '—')}")
    lines.append(f"- Python：{environment.get('python_version', '—')}")
    lines.append("")
    lines.append("## 总判")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 门禁结论 | **{_cell(aggregate.get('verdict'), 20)}** |")
    for key in sorted(aggregate):
        lines.append(f"| {_cell(key, 40)} | {_cell(aggregate[key], 40)} |")
    for key in sorted(scope):
        lines.append(f"| 证据域 `{_cell(key, 40)}` | {_cell(scope[key], 40)} |")
    lines.append("")

    lines.append("## 门禁检查（M0–M15 / WP00–WP04 / P1）")
    lines.append("")
    lines.append(
        "| 检查 | PRD | 需求 | 层级 | 实现状态 | Windows/模拟 | 运行器验证 | "
        "现场（Windows 实测） | 证据路径 | 阻断原因 |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for entry in checks:
        check = entry.get("check") or {}
        verdict = entry.get("verdict") or {}
        fields = _fields(entry)
        lines.append(
            "| {cid} | {prd} | {req} | {tier} | {impl} | {win} | {runner} | "
            "{onsite} | {ev} | {blocker} |".format(
                cid=f"`{check.get('check_id', '—')}`",
                prd=_cell(check.get("prd_citation"), 28),
                req=_cell(check.get("requirement"), 60),
                tier=_cell(check.get("tier"), 12),
                impl=_cell(fields.get("implementation"), 16),
                win=_cell(fields.get("windows_status"), 16),
                runner=_cell(_verdict_kind(verdict), 14),
                onsite=_cell(fields.get("onsite_status"), 16),
                ev=_evidence_paths(verdict),
                blocker=_cell(fields.get("blocker"), 90),
            )
        )
    lines.append("")

    lines.append("## 端到端用例（PRD §5.2 十条）")
    lines.append("")
    lines.append("| 用例 | PRD | 运行器验证 | 理由 | 关联检查 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for case in cases:
        verdict = case.get("verdict") or {}
        lines.append(
            "| {cid} | {prd} | {kind} | {reasons} | {checks} |".format(
                cid=f"`{case.get('case_id', '—')}`",
                prd=_cell(case.get("prd_citation"), 28),
                kind=_cell(_verdict_kind(verdict), 14),
                reasons=_cell("; ".join(_reasons(case, verdict)), 110),
                checks=_cell(", ".join(case.get("check_ids") or ()), 40),
            )
        )
    lines.append("")

    gaps = bundle.get("coverage_gaps") or ()
    lines.append("## 覆盖缺口")
    lines.append("")
    if gaps:
        for gap in gaps:
            lines.append(f"- {_cell(gap, 300)}")
    else:
        lines.append("（无：门禁未登记任何覆盖缺口）")
    lines.append("")

    unverified = bundle.get("field_unverified") or ()
    lines.append("## 未验证字段")
    lines.append("")
    if unverified:
        for item in unverified:
            lines.append(f"- {_cell(item, 300)}")
    else:
        lines.append("（无）")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "生成方式：`python scripts/windows/build-coverage-table.py`。"
        "四列状态互不替代：实现完成不等于模拟通过，模拟通过不等于运行器验证，"
        "运行器验证不等于 Windows 真实验收。"
    )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--bundle",
        type=Path,
        default=None,
        help="Read an existing acceptance.json instead of running the gate.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write here instead of stdout.",
    )
    args = parser.parse_args()

    if args.bundle is not None:
        import json

        bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
        run_id = str(bundle.get("run_id") or args.bundle.parent.name)
    else:
        # The run directory is under local-private/ by design (PRD 12): the
        # bundle carries host fingerprints and evidence paths and must not be
        # committed.  The rendered table is what gets committed.
        run_root = REPO_ROOT / "local-private" / "acceptance-runs" / BUNDLE_DIRNAME
        run_root.mkdir(parents=True, exist_ok=True)
        inputs = registry.build_inputs(run_root)
        outcome = AcceptanceRunner(
            run_dir=run_root,
            checks=inputs.checks,
            probes=inputs.probes,
            environment=inputs.environment,
            cases=inputs.cases,
            milestones=inputs.milestones,
            measurements=inputs.measurements,
            coverage_gaps=inputs.coverage_gaps,
        ).run()
        import json

        bundle = json.loads((run_root / "acceptance.json").read_text(encoding="utf-8"))
        run_id = outcome.bundle.run_id if hasattr(outcome, "bundle") else BUNDLE_DIRNAME

    text = render(bundle, run_id)
    _assert_no_user_data(text, _forbidden_fragments())

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"wrote {args.output} ({len(text)} bytes, {len(text.splitlines())} lines)")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
