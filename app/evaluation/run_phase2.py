"""第十阶段第二阶段：真实 Evaluation Benchmark 运行入口（CLI）。

流程：加载固定 Dataset → 逐 case 真实 Agent 采样 → Deterministic Validator
→ LLM-as-Judge → 语义漏检专项扫描 → 生成 Evaluation Report（JSON + Markdown）。

用法（在项目根目录运行）：
  python app/evaluation/run_phase2.py                 # 完整运行
  python app/evaluation/run_phase2.py --limit 2       # 只跑前 2 个 case
  python app/evaluation/run_phase2.py --only P2B-001  # 只跑指定 case（可逗号分隔）
  python app/evaluation/run_phase2.py --resume        # 跳过已完成 case（断点续跑）
  python app/evaluation/run_phase2.py --skip-gaps     # 跳过语义漏检专项扫描
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

from openai import OpenAI

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_URL = "https://api.deepseek.com"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = PROJECT_ROOT / "app/evaluation/cases/phase2_dataset.json"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "app/evaluation/reports/phase2"


def _load_dataset(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data


def _create_client() -> OpenAI:
    import os

    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY")
    # 显式超时，避免单次 API 调用无响应时整个运行挂起（默认 600s 过长）。
    return OpenAI(api_key=api_key, base_url=BASE_URL, timeout=60.0)


def _load_existing(results_path: Path) -> Dict[str, Dict[str, Any]]:
    if results_path.exists():
        try:
            data = json.loads(results_path.read_text(encoding="utf-8"))
            return {r["case_id"]: r for r in data.get("results", [])}
        except (json.JSONDecodeError, KeyError):
            return {}
    return {}


def _save_progress(results_path: Path, results: List[Dict[str, Any]]) -> None:
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(
        json.dumps({"results": results}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 2 真实 Evaluation Benchmark")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--limit", type=int, default=None, help="最多运行 N 个 case")
    parser.add_argument("--only", type=str, default=None, help="逗号分隔的 case_id 白名单")
    parser.add_argument("--resume", action="store_true", help="跳过已完成 case")
    parser.add_argument("--skip-gaps", action="store_true", help="跳过语义漏检专项扫描")
    args = parser.parse_args()

    from .judge_llm import judge_case
    from .phase2_runner import build_report, run_dual_evaluation, write_reports
    from .sampling import sample_agent_run
    from .semantic_gap import scan_semantic_gaps

    dataset = _load_dataset(args.dataset)
    cases = dataset["cases"]
    if args.only:
        allowed = {c.strip() for c in args.only.split(",") if c.strip()}
        cases = [c for c in cases if c["case_id"] in allowed]
    if args.limit is not None:
        cases = cases[: args.limit]
    print(f"Dataset：{dataset.get('benchmark')} v{dataset.get('version')}，本次运行 {len(cases)} 个 case")

    results_path = args.report_dir / "progress_results.json"
    existing = _load_existing(results_path) if args.resume else {}
    if existing:
        print(f"续跑：跳过 {len(existing)} 个已完成 case：{sorted(existing)}")

    agent_client = _create_client()
    judge_client = _create_client()

    results: List[Dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        case_id = case["case_id"]
        if case_id in existing:
            results.append(existing[case_id])
            continue
        print(f"\n[{index}/{len(cases)}] === 运行 {case_id}（{case['category_name']}）===")
        print(f"  问题：{case['question']}")
        result = run_dual_evaluation(agent_client, judge_client, case)
        results.append(result)
        _save_progress(results_path, results)
        det = result["deterministic_result"]["score"]["overall"]
        judge = result["llm_judge_result"].get("overall_score")
        print(
            f"  → final_status={result['final_status']} final_score={result['final_score']} "
            f"det={det} judge={judge} conflict={result['validator_judge_conflict']}"
        )
        if result["agent_record"].get("error"):
            print(f"  ⚠ Agent 错误：{result['agent_record']['error']}")

    if not args.skip_gaps:
        print(f"\n=== 语义漏检专项扫描（{len(results)} 个 case）===")
        gap_results: List[Dict[str, Any]] = []
        for result in results:
            gap = scan_semantic_gaps(judge_client, {"case_id": result["case_id"], "question": result["question"]}, result["agent_record"])
            gap_results.append(gap)
            print(f"  {gap['case_id']}: findings={len(gap.get('findings', []))} regex_hits={len(gap.get('regex_hits', []))}")
    else:
        gap_results = []

    report = build_report(results, gap_results, dataset)
    directory = write_reports(report, results, report_dir=str(args.report_dir))
    s = report["summary"]
    print("\n=== 运行完成 ===")
    print(
        f"总计 {s['total']} 个 case：PASS {s['passed']} / FAIL {s['failed']}，"
        f"通过率 {s['pass_rate']:.2%}，冲突 {s['conflicts_count']} 个"
    )
    print(f"报告目录：{directory}")


if __name__ == "__main__":
    main()
