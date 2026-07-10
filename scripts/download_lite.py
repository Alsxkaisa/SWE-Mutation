import json
from pathlib import Path

try:
    from datasets import load_dataset
except ImportError:
    print("请先安装 datasets: pip install datasets")
    exit(1)

OUTPUT = Path("data") / "instances_lite.jsonl"

def main():
    print("正在从 HuggingFace 下载 SWE-bench_Lite ...")
    ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        for row in ds:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"下载完成: {len(ds)} 个实例 -> {OUTPUT}")

if __name__ == "__main__":
    main()
