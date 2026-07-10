import json
from collections import defaultdict
from pathlib import Path

DATA_DIR = Path("data")
RESULTS_DIR = Path("results")
MUTANTS_SRC = DATA_DIR / "curated_mutations.jsonl"
INSTANCES_OUT = DATA_DIR / "instances_lite.jsonl"
MUTANTS_OUT = RESULTS_DIR / "mutants" / "preds.json"

HF_LITE_URL = "https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite/resolve/main/data/train-00000-of-00001.parquet"

def download_lite():
    """尝试多种方式下载 SWE-bench_Lite"""
    import subprocess, sys, shutil, urllib.request, ssl

    ctx = ssl.create_default_context()
    url = "https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite/resolve/main/data/train-00000-of-00001.parquet"
    parquet_path = DATA_DIR / "swe_bench_lite.parquet"

    print("正在尝试下载 SWE-bench_Lite ...")

    if shutil.which("wget"):
        result = subprocess.run(
            ["wget", str(url), "-O", str(parquet_path)],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode == 0:
            print("wget 下载成功")
            return parquet_path
    elif shutil.which("curl"):
        result = subprocess.run(
            ["curl", "-L", str(url), "-o", str(parquet_path)],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode == 0:
            print("curl 下载成功")
            return parquet_path

    try:
        print("尝试 urllib 下载 ...")
        with urllib.request.urlopen(url, context=ctx, timeout=120) as resp:
            with open(parquet_path, "wb") as f:
                f.write(resp.read())
        print("urllib 下载成功")
        return parquet_path
    except Exception as e:
        print(f"urllib 下载失败: {e}")

    return None

def convert_parquet_to_jsonl(parquet_path):
    """将 parquet 转换为 JSONL 格式"""
    try:
        import pandas as pd
        print(f"正在转换 {parquet_path} -> JSONL ...")
        df = pd.read_parquet(parquet_path)
        lite_ids = set()
        with open(INSTANCES_OUT, "w", encoding="utf-8") as f:
            for _, row in df.iterrows():
                record = row.to_dict()
                record = {k: (v.item() if hasattr(v, "item") else v) for k, v in record.items()}
                for k, v in record.items():
                    if isinstance(v, bytes):
                        record[k] = v.decode("utf-8", errors="replace")
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                lite_ids.add(record.get("instance_id", ""))
        return lite_ids
    except ImportError:
        print("pandas 未安装，尝试用 datasets 库读取 parquet ...")
        try:
            from datasets import Dataset
            ds = Dataset.from_parquet(str(parquet_path))
            lite_ids = set()
            with open(INSTANCES_OUT, "w", encoding="utf-8") as f:
                for row in ds:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    lite_ids.add(row["instance_id"])
            return lite_ids
        except Exception as e2:
            print(f"读取 parquet 失败: {e2}")
            return None

def load_jsonl_ids(path):
    """从已有的 JSONL 文件加载 instance_ids"""
    ids = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            ids.add(row.get("instance_id", ""))
    return ids

def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "mutants").mkdir(parents=True, exist_ok=True)

    # Step 1: 获取 SWE-bench_Lite 的 instance_ids
    lite_ids = set()

    if INSTANCES_OUT.exists():
        print(f"发现已有缓存: {INSTANCES_OUT}")
        lite_ids = load_jsonl_ids(INSTANCES_OUT)
        print(f"已加载 {len(lite_ids)} 个实例 ID")

    # 尝试手动指定的本地文件
    manual_file = DATA_DIR / "swe_bench_lite.jsonl"
    if not lite_ids and manual_file.exists():
        print(f"发现本地文件: {manual_file}")
        import shutil
        shutil.copy(manual_file, INSTANCES_OUT)
        lite_ids = load_jsonl_ids(INSTANCES_OUT)
        print(f"已加载 {len(lite_ids)} 个实例 ID")

    if not lite_ids:
        parquet_path = download_lite()
        if parquet_path and parquet_path.exists():
            ids = convert_parquet_to_jsonl(parquet_path)
            if ids:
                lite_ids = ids

    if not lite_ids:
        print()
        print("=" * 60)
        print("无法自动下载 SWE-bench_Lite，请手动下载:")
        print("=" * 60)
        print()
        print("方式 1: 浏览器打开以下链接下载 parquet 文件")
        print(f"  {HF_LITE_URL}")
        print(f"  下载后保存到 {DATA_DIR / 'swe_bench_lite.parquet'}")
        print()
        print("方式 2: 使用 huggingface-cli")
        print("  pip install huggingface-hub")
        print("  huggingface-cli download princeton-nlp/SWE-bench_Lite --local-dir ./data")
        print()
        print("方式 3: 自己准备一个 JSONL 格式的 patches 文件")
        print("  将文件保存为 data/swe_bench_lite.jsonl")
        print("  每行是一个 JSON 对象，必须包含 instance_id, repo, version, patch, test_patch, test_files 等字段")
        print()
        print("下载后重新运行本脚本即可。")
        print("=" * 60)

    # Step 2: 转换 curated_mutations.jsonl（这个脚本总是可以做）
    if not MUTANTS_SRC.exists():
        print(f"\n错误: 未找到 {MUTANTS_SRC}")
        return

    print(f"\n正在读取 {MUTANTS_SRC} ...")
    mutants = defaultdict(list)
    curated_ids = set()
    with open(MUTANTS_SRC, encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            iid = row["instance_id"]
            curated_ids.add(iid)
            if not lite_ids or iid in lite_ids:
                mutants[iid].append({
                    "diff": row["mutation"]["diff"],
                    "strategy_group": row["mutation"]["strategy_group"],
                    "strategy_code": row["mutation"]["strategy_code"],
                    "explanation": row["mutation"]["explanation"]
                })

    print(f"curated_mutations 总实例数: {len(curated_ids)}")
    if lite_ids:
        print(f"与 Lite 的交集: {len(mutants)} 个实例")
    else:
        print(f"转换所有实例: {len(mutants)} 个实例 (lite_ids 未加载)")

    if len(mutants) == 0:
        print("警告: 无交集，将转换所有 curated 实例作为备选")
        mutants.clear()
        with open(MUTANTS_SRC, encoding="utf-8") as f:
            for line in f:
                row = json.loads(line)
                iid = row["instance_id"]
                mutants[iid].append({
                    "diff": row["mutation"]["diff"],
                    "strategy_group": row["mutation"]["strategy_group"],
                    "strategy_code": row["mutation"]["strategy_code"],
                    "explanation": row["mutation"]["explanation"]
                })

    # Step 3: 转换为 preds.json 格式
    preds = {}
    for iid, mlist in mutants.items():
        preds[iid] = {
            "model_name_or_path": "curated",
            "instance_id": iid,
            "model_patch": json.dumps({"mutations": mlist})
        }

    MUTANTS_OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(MUTANTS_OUT, "w", encoding="utf-8") as f:
        json.dump(preds, f, indent=2, ensure_ascii=False)

    total_mutations = sum(len(v) for v in mutants.values())
    print(f"\n变异体数据转换完成: {len(preds)} 个实例, {total_mutations} 条变异体")
    print(f"  -> {MUTANTS_OUT}")

    if lite_ids and INSTANCES_OUT.exists():
        print(f"\n==== 使用说明 ====")
        print(f"patches_file (实例元数据): {INSTANCES_OUT}")
        print(f"mutants_file (变异体):     {MUTANTS_OUT}")
        print(f"需要你额外准备: test_preds_file (模型生成的测试套件)")
        print()
        print("评估命令示例:")
        print(f"  python -m evaluation.evaluate ^")
        print(f"    --patches-file {INSTANCES_OUT} ^")
        print(f"    --mutants-file {MUTANTS_OUT} ^")
        print(f"    --test-preds-file results/my_model/preds.json ^")
        print(f"    --task test_repair ^")
        print(f"    -o eval_results ^")
        print(f"    --workers 4")

if __name__ == "__main__":
    main()
