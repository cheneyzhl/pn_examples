# -*- coding: utf-8 -*-
"""
多进程并行跑 boundary coverage 检测，输入/输出与 check_boundary_coverage.py 对齐。

- 将待评估规则列表分片，多进程各跑一段，最后合并 result/、work/ 与 summary.json。
- 通过子进程调用 check_boundary_coverage.py，不修改其核心逻辑。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
from argparse import Namespace
from typing import Any, Dict, List, Optional, Sequence, Tuple

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_CHECK_SCRIPT = os.path.join(_SCRIPT_DIR, "check_boundary_coverage.py")

if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from check_boundary_coverage import (  # noqa: E402
    _build_rule_to_data_name_map,
    _load_json,
    _safe_listdir,
    _write_summary_json,
)

# 写入 worker.log 时只保留脚本主动打印的状态行（与 Calibre 海量 stdout 区分）
_WORKER_LOG_STATUS_PREFIXES: Tuple[str, ...] = (
    "[skip]",
    "[run]",
    "[corner]",
    "[sample]",
    "[prep]",
    "[resume]",
    "[WARN]",
    "[INFO]",
    "[OK]",
    "[ERR]",
)


def _is_worker_status_line(line: str) -> bool:
    s = line.lstrip()
    if not s.startswith("["):
        return False
    for p in _WORKER_LOG_STATUS_PREFIXES:
        if s.startswith(p):
            return True
    return False


def _pump_worker_stdout(
    proc: subprocess.Popen,
    prefix: str,
    log_f: Any,
    *,
    tee_terminal: bool,
) -> None:
    """子进程 stdout 行级转发；终端可 tee 全量，worker.log 仅写入状态行。"""
    if proc.stdout is None:
        return
    try:
        for line in iter(proc.stdout.readline, ""):
            if line == "" and proc.poll() is not None:
                break
            if tee_terminal:
                sys.stdout.write(f"{prefix}{line}")
                sys.stdout.flush()
            if _is_worker_status_line(line):
                log_f.write(line)
                log_f.flush()
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass


def _split_into_chunks(items: List[Any], n_jobs: int) -> List[List[Any]]:
    if not items:
        return []
    n_jobs = max(1, min(n_jobs, len(items)))
    k = len(items)
    base = k // n_jobs
    rem = k % n_jobs
    chunks: List[List[Any]] = []
    idx = 0
    for i in range(n_jobs):
        size = base + (1 if i < rem else 0)
        if size <= 0:
            continue
        chunks.append(items[idx : idx + size])
        idx += size
    return chunks


def _build_rule_tasks_baseline_summary(
    baseline_summary: str,
    generated_scripts_dir: str,
    max_rules: int,
) -> List[Tuple[str, str, Dict[str, Any]]]:
    from config import NEW_DATASETS_DIR

    summary_data = _load_json(baseline_summary)
    per_rule_list = summary_data.get("per_rule", [])
    rule_to_data = _build_rule_to_data_name_map(NEW_DATASETS_DIR)
    rule_tasks: List[Tuple[str, str, Dict[str, Any]]] = []
    for pr in per_rule_list:
        rule_name = pr.get("rule_name", "")
        if not rule_name:
            continue
        data_name = rule_to_data.get(rule_name)
        if not data_name:
            continue
        gen_rule_dir = os.path.join(generated_scripts_dir, data_name, rule_name)
        if not os.path.isdir(gen_rule_dir) or not os.path.isfile(
            os.path.join(gen_rule_dir, "rule_meta.json")
        ):
            continue
        rule_tasks.append((rule_name, data_name, pr))
    if max_rules > 0:
        rule_tasks = rule_tasks[:max_rules]
    return rule_tasks


def _build_rule_tasks_baseline_result_dir(
    data_name: str,
    baseline_result_dir: str,
    generated_scripts_dir: str,
    max_rules: int,
) -> List[Tuple[str, str, Dict[str, Any]]]:
    generated_data_dir = os.path.join(generated_scripts_dir, data_name)
    if not os.path.isdir(generated_data_dir):
        raise ValueError(f"generated_scripts_dir data_name dir not found: {generated_data_dir}")

    rule_names: List[str] = []
    for name in _safe_listdir(generated_data_dir):
        rule_dir = os.path.join(generated_data_dir, name)
        if os.path.isdir(rule_dir) and os.path.isfile(os.path.join(rule_dir, "rule_meta.json")):
            rule_names.append(name)
    rule_names.sort()
    rule_tasks: List[Tuple[str, str, Dict[str, Any]]] = []
    for rule_name in rule_names:
        baseline_path = os.path.join(baseline_result_dir, f"{rule_name}.json")
        if not os.path.isfile(baseline_path):
            continue
        pr = _load_json(baseline_path)
        if not isinstance(pr, dict):
            continue
        rule_tasks.append((rule_name, data_name, pr))
    if max_rules > 0:
        rule_tasks = rule_tasks[:max_rules]
    return rule_tasks


def _seed_worker_from_final(
    final_output_dir: str,
    worker_output_dir: str,
    rule_names: Sequence[str],
) -> None:
    """把最终 output_dir 里已有结果拷到 worker 目录，便于 --skip_existing / --resume。"""
    for rn in rule_names:
        src_eval = os.path.join(final_output_dir, "result", rn, "eval.json")
        if os.path.isfile(src_eval):
            dst_eval = os.path.join(worker_output_dir, "result", rn, "eval.json")
            os.makedirs(os.path.dirname(dst_eval), exist_ok=True)
            shutil.copy2(src_eval, dst_eval)
        src_ck = os.path.join(final_output_dir, "result", rn, "eval.checkpoint.json")
        if os.path.isfile(src_ck):
            dst_ck = os.path.join(worker_output_dir, "result", rn, "eval.checkpoint.json")
            os.makedirs(os.path.dirname(dst_ck), exist_ok=True)
            shutil.copy2(src_ck, dst_ck)
        w_final = os.path.join(final_output_dir, "work")
        if os.path.isdir(w_final):
            for dn in _safe_listdir(w_final):
                wsrc = os.path.join(w_final, dn, rn)
                if os.path.isdir(wsrc):
                    wdst = os.path.join(worker_output_dir, "work", dn, rn)
                    if os.path.isdir(wdst):
                        shutil.rmtree(wdst, ignore_errors=True)
                    os.makedirs(os.path.dirname(wdst), exist_ok=True)
                    shutil.copytree(wsrc, wdst)


def _write_shard_baseline_summary(path: str, per_rule: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"per_rule": per_rule}, f, ensure_ascii=False, indent=2)


def _merge_worker_outputs(
    final_output_dir: str,
    worker_dirs: List[str],
) -> None:
    for wd in worker_dirs:
        res_root = os.path.join(wd, "result")
        if not os.path.isdir(res_root):
            continue
        for rn in _safe_listdir(res_root):
            src_rule = os.path.join(res_root, rn)
            dst_rule = os.path.join(final_output_dir, "result", rn)
            if not os.path.isdir(src_rule):
                continue
            os.makedirs(dst_rule, exist_ok=True)
            for fn in ("eval.json", "eval.checkpoint.json"):
                sp = os.path.join(src_rule, fn)
                if os.path.isfile(sp):
                    shutil.copy2(sp, os.path.join(dst_rule, fn))
        work_root = os.path.join(wd, "work")
        if os.path.isdir(work_root):
            for dn in _safe_listdir(work_root):
                sp_data = os.path.join(work_root, dn)
                if not os.path.isdir(sp_data):
                    continue
                for rn in _safe_listdir(sp_data):
                    wsrc = os.path.join(sp_data, rn)
                    wdst = os.path.join(final_output_dir, "work", dn, rn)
                    if not os.path.isdir(wsrc):
                        continue
                    if os.path.isdir(wdst):
                        shutil.rmtree(wdst, ignore_errors=True)
                    os.makedirs(os.path.dirname(wdst), exist_ok=True)
                    shutil.copytree(wsrc, wdst)


def _merge_skipped_rules(worker_dirs: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for wd in worker_dirs:
        sp = os.path.join(wd, "summary.json")
        if not os.path.isfile(sp):
            continue
        try:
            s = _load_json(sp)
            for x in s.get("skipped_rules", []) or []:
                out.append(x)
        except Exception:
            pass
    return out


def _build_final_details_and_counts(
    rule_tasks: List[Tuple[str, str, Dict[str, Any]]],
    final_output_dir: str,
    skipped_rules: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int, int, int]:
    skipped_names = {x.get("rule_name") for x in skipped_rules if x.get("rule_name")}
    details: List[Dict[str, Any]] = []
    correct_by_judge = 0
    match_passed = 0
    detect_passed = 0
    for rule_name, _dn, _pr in rule_tasks:
        if rule_name in skipped_names:
            continue
        ep = os.path.join(final_output_dir, "result", rule_name, "eval.json")
        if not os.path.isfile(ep):
            continue
        ev = _load_json(ep)
        details.append(ev)
        if ev.get("all_corners_ok"):
            correct_by_judge += 1
        if ev.get("all_corners_match"):
            match_passed += 1
        if ev.get("all_corners_detect"):
            detect_passed += 1
    return details, correct_by_judge, match_passed, detect_passed


def _worker_cmd(
    check_args: Namespace,
    shard_summary_path: str,
    worker_output_dir: str,
) -> List[str]:
    cmd: List[str] = [
        sys.executable,
        _CHECK_SCRIPT,
        "--baseline_summary",
        shard_summary_path,
        "--generated_scripts_dir",
        check_args.generated_scripts_dir,
        "--output_dir",
        worker_output_dir,
        "--judge_mode",
        check_args.judge_mode,
        "--drc_report_name",
        check_args.drc_report_name,
        "--max_rules",
        "0",
    ]
    if check_args.skip_existing:
        cmd.append("--skip_existing")
    if check_args.resume:
        cmd.append("--resume")
    if getattr(check_args, "fail_on_missing_pos", False):
        cmd.append("--fail_on_missing_pos")
    else:
        if getattr(check_args, "skip_rules_with_missing_pos", True):
            cmd.append("--skip_rules_with_missing_pos")
    if getattr(check_args, "verbose_progress", False):
        cmd.append("--verbose_progress")
    return cmd


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="多进程并行跑 boundary coverage（子进程调用 check_boundary_coverage.py），输出与单进程一致。"
    )
    p.add_argument("--jobs", type=int, default=4, help="并行 worker 数量（默认 4）")
    p.add_argument(
        "--keep_worker_dirs",
        action="store_true",
        help="保留各 worker 临时目录（默认跑完后删除 _parallel_tmp）",
    )
    p.add_argument("--data_name", type=str, choices=["freePDK15", "asap7", "freepdk-45nm"], default="")
    p.add_argument("--baseline_result_dir", type=str, default="")
    p.add_argument("--baseline_summary", type=str, default="")
    p.add_argument("--generated_scripts_dir", type=str, required=True)
    p.add_argument("--output_dir", type=str, required=True)
    p.add_argument("--judge_mode", type=str, choices=["detect", "match"], default="match")
    p.add_argument("--drc_report_name", type=str, default="drc_report")
    p.add_argument("--max_rules", type=int, default=0)
    p.add_argument("--skip_existing", action="store_true")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--skip_rules_with_missing_pos", action="store_true", default=True)
    p.add_argument("--fail_on_missing_pos", action="store_true")
    p.add_argument(
        "--quiet",
        action="store_true",
        help="子进程输出不打印到终端，仅写入各 worker 目录下 worker.log（旧行为）。",
    )
    p.add_argument(
        "--no_verbose_progress",
        action="store_true",
        help="不传递 --verbose_progress 给子进程（不打印 corner 级进度行）。",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.resume:
        args.skip_existing = True
    # 并行默认打开 corner 进度，与「总控可见」一致；可用 --no_verbose_progress 关闭
    args.verbose_progress = not args.no_verbose_progress

    from config import BASELINE_RESULT_DIR

    final_out = os.path.abspath(args.output_dir)
    os.makedirs(final_out, exist_ok=True)

    if args.baseline_summary and os.path.isfile(args.baseline_summary):
        rule_tasks = _build_rule_tasks_baseline_summary(
            args.baseline_summary,
            args.generated_scripts_dir,
            args.max_rules,
        )
    else:
        data_name = args.data_name or "freePDK15"
        bdir = args.baseline_result_dir or BASELINE_RESULT_DIR
        rule_tasks = _build_rule_tasks_baseline_result_dir(
            data_name,
            bdir,
            args.generated_scripts_dir,
            args.max_rules,
        )

    if not rule_tasks:
        print("[WARN] 无待评估规则。")
        skipped: List[Dict[str, Any]] = []
        _write_summary_json(
            final_out, args, [], 0, 0, 0, skipped
        )
        print("[OK] Completed. accuracy=0.0 (0/0)")
        return

    n_jobs = max(1, args.jobs)
    chunks = _split_into_chunks(rule_tasks, n_jobs)
    tmp_root = os.path.join(final_out, "_parallel_tmp")
    os.makedirs(tmp_root, exist_ok=True)

    print(f"[parallel] 总规则数={len(rule_tasks)}，worker 数={len(chunks)}，输出目录={final_out}")

    worker_dirs: List[str] = []
    procs: List[Tuple[int, subprocess.Popen, Any, Optional[threading.Thread]]] = []
    wi = 0
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")

    for chunk in chunks:
        if not chunk:
            continue
        wdir = os.path.join(tmp_root, f"worker_{wi:02d}")
        os.makedirs(wdir, exist_ok=True)
        worker_dirs.append(wdir)
        per_rule_only = [c[2] for c in chunk]
        shard_path = os.path.join(wdir, "baseline_shard.json")
        _write_shard_baseline_summary(shard_path, per_rule_only)
        rule_names = [c[0] for c in chunk]
        _seed_worker_from_final(final_out, wdir, rule_names)
        log_path = os.path.join(wdir, "worker.log")
        log_f = open(log_path, "w", encoding="utf-8")
        prefix = f"[w{wi:02d}] "
        cmd = _worker_cmd(args, shard_path, wdir)
        if args.quiet:
            p = subprocess.Popen(
                cmd,
                cwd=_SCRIPT_DIR,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            thr = threading.Thread(
                target=_pump_worker_stdout,
                args=(p, prefix, log_f),
                kwargs={"tee_terminal": False},
                daemon=True,
            )
            thr.start()
            print(f"[parallel] 启动 worker_{wi:02d}，规则数={len(chunk)}（安静模式，状态行→{log_path}）")
        else:
            p = subprocess.Popen(
                cmd,
                cwd=_SCRIPT_DIR,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            thr = threading.Thread(
                target=_pump_worker_stdout,
                args=(p, prefix, log_f),
                kwargs={"tee_terminal": True},
                daemon=True,
            )
            thr.start()
            print(
                f"[parallel] 启动 worker_{wi:02d}，规则数={len(chunk)} "
                f"（终端前缀 {prefix.strip()}，状态行写入 {log_path}）"
            )
        procs.append((wi, p, log_f, thr))
        wi += 1

    rc = 0
    for idx, p, log_f, thr in procs:
        code = p.wait()
        if thr is not None:
            thr.join(timeout=600)
        try:
            log_f.close()
        except Exception:
            pass
        if code != 0:
            rc = code
            print(f"[ERR] worker_{idx:02d} 退出码 {code}", file=sys.stderr)

    if rc != 0:
        sys.exit(rc)

    _merge_worker_outputs(final_out, worker_dirs)
    merged_skipped = _merge_skipped_rules(worker_dirs)
    details, cj, mp, dp = _build_final_details_and_counts(
        rule_tasks, final_out, merged_skipped
    )
    _write_summary_json(final_out, args, details, cj, mp, dp, merged_skipped)

    if not args.keep_worker_dirs:
        shutil.rmtree(tmp_root, ignore_errors=True)

    total = len(details)
    acc = (cj / total) if total else 0.0
    print(f"[OK] Completed (parallel). accuracy={acc} ({cj}/{total})")
    if merged_skipped:
        print(f"[parallel] 合并后跳过规则数 skipped_rule_count={len(merged_skipped)}（缺 pos 等）:")
        for x in merged_skipped:
            rn = x.get("rule_name", "")
            typ = x.get("type", "")
            print(f"  - {rn}  type={typ}")
    else:
        print("[parallel] 合并后无 skipped_rules 条目。")


if __name__ == "__main__":
    main()
