
# -*- coding: utf-8 -*-
"""并行调度脚本：按规则批量并行运行 run_baseline.py。

- 从 all_rules_freePDK15_asap7.json 中选取 freePDK15 前 N 条和 asap7 前 M 条规则；
- 按进程数上限并行启动多个 run_baseline.py，每个进程处理一批规则；
- run_baseline 本身会按规则输出结果到 result/<rule_name>.json，并输出日志到 log/<timestamp>/<rule_name>.txt。
"""
import os
import sys
import json
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_all_rules(path: str):
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return data  # dict: name -> {rule, layers, process}


def select_rules(all_rules, free_n: int, asap_n: int):
    free = []
    asap = []
    for name, info in all_rules.items():
        proc = info.get('process')
        item = {
            'rule_name': name,
            'rule': info.get('rule', ''),
            'layers': info.get('layers', []),
        }
        if proc == 'freePDK15' and len(free) < free_n:
            free.append(item)
        elif proc == 'asap7' and len(asap) < asap_n:
            asap.append(item)
        if len(free) >= free_n and len(asap) >= asap_n:
            break
    return free, asap


def chunk_list(items, chunk_size):
    if chunk_size <= 0:
        chunk_size = 1
    for i in range(0, len(items), chunk_size):
        yield items[i:i + chunk_size]


def run_job(rules_batch, process_name, base_rul, data_name, log_dir, result_dir, idx):
    """在子进程中调用 run_baseline.py 处理一批规则。"""
    if not rules_batch:
        return f'{process_name}-batch-{idx}: empty batch, skipped'

    # 为该批次创建临时规则文件
    tmp_rules_dir = os.path.join(SCRIPT_DIR, 'tmp_rules')
    os.makedirs(tmp_rules_dir, exist_ok=True)
    tmp_rules_path = os.path.join(tmp_rules_dir, f'rules_{process_name}_{idx}.json')
    with open(tmp_rules_path, 'w', encoding='utf-8') as f:
        json.dump(rules_batch, f, ensure_ascii=False, indent=2)

    cmd = [
        sys.executable,
        os.path.join(SCRIPT_DIR, 'run_baseline.py'),
        '--base_rul', base_rul,
        '--rules', tmp_rules_path,
        '--data_name', data_name,
        '--log_dir', log_dir,
        '--result_dir', result_dir,
    ]

    proc = subprocess.run(cmd, cwd=SCRIPT_DIR)
    return f'{process_name}-batch-{idx}: exit_code={proc.returncode}'


def main():
    parser = argparse.ArgumentParser(description='并行运行 run_baseline.py 进行规则基准测试')
    parser.add_argument('--rules_json', type=str, default='all_rules_freePDK15_asap7.json',
                        help='合并后的规则 JSON（包含 process 字段）')
    parser.add_argument('--free_n', type=int, default=100, help='选取 freePDK15 前 N 条规则')
    parser.add_argument('--asap_n', type=int, default=100, help='选取 asap7 前 N 条规则')
    parser.add_argument('--concurrency', type=int, default=20, help='最大并发 run_baseline 进程数')
    parser.add_argument('--rules_per_job', type=int, default=10, help='每个 run_baseline 进程处理的规则数')
    parser.add_argument('--log_dir', type=str, default='', help='日志根目录（默认 baseline_direct_coord/log）')
    parser.add_argument('--result_dir', type=str, default='', help='规则结果输出根目录（默认 baseline_direct_coord/result）')
    args = parser.parse_args()

    rules_json_path = args.rules_json
    if not os.path.isabs(rules_json_path):
        rules_json_path = os.path.join(SCRIPT_DIR, rules_json_path)
    if not os.path.isfile(rules_json_path):
        print('规则文件不存在:', rules_json_path)
        sys.exit(1)

    all_rules = load_all_rules(rules_json_path)
    free_rules, asap_rules = select_rules(all_rules, args.free_n, args.asap_n)
    print(f'selected freePDK15 rules: {len(free_rules)}, asap7 rules: {len(asap_rules)}')

    log_dir = args.log_dir or os.path.join(SCRIPT_DIR, 'log')
    result_dir = args.result_dir or os.path.join(SCRIPT_DIR, 'result')

    jobs = []
    # freePDK15 任务
    free_base_rul = os.path.join(os.path.dirname(SCRIPT_DIR), 'datasets', 'freePDK15', 'calibreDRC.rul')
    for idx, batch in enumerate(chunk_list(free_rules, args.rules_per_job)):
        jobs.append(('freePDK15', batch, free_base_rul, 'freePDK15', idx))

    # asap7 任务
    asap_base_rul = os.path.join(os.path.dirname(SCRIPT_DIR), 'datasets', 'asap7', 'calibreDRC.rul')
    for idx, batch in enumerate(chunk_list(asap_rules, args.rules_per_job)):
        jobs.append(('asap7', batch, asap_base_rul, 'asap7', idx))

    if not jobs:
        print('没有可执行的规则任务')
        return

    max_workers = max(1, args.concurrency)
    print(f'start jobs: {len(jobs)}, concurrency={max_workers}')

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = []
        for process_name, batch, base_rul, data_name, idx in jobs:
            futures.append(ex.submit(
                run_job,
                batch,
                process_name,
                base_rul,
                data_name,
                log_dir,
                result_dir,
                idx,
            ))
        for fut in as_completed(futures):
            try:
                print(fut.result())
            except Exception as e:
                print('job failed:', e)


if __name__ == '__main__':
    main()
