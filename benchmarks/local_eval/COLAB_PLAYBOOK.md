# Colab (`1×T4`) & Kaggle (`2×T4`) Execution Playbook

Copy-paste these cells into a **Google Colab T4** notebook to run Steps 1, 2, and 3 end-to-end without Docker.

---

## Step 1 — 30-min Smoke Test (`Qwen2.5-Coder-7B-Instruct Q4_K_M` via `llama-server`)

### Cell 1: Clone Argus & install `llama-cpp-python` (server) or prebuilt `llama-server`
```bash
!git clone https://github.com/hemang1404/Argus.git
%cd Argus
!pip install -q -e .
!pip install -q "llama-cpp-python[server]" huggingface_hub datasets
```

### Cell 2: Download `Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf` & start server on `127.0.0.1:8000`
```python
import subprocess, time
from huggingface_hub import hf_hub_download

gguf_path = hf_hub_download(
    repo_id="Qwen/Qwen2.5-Coder-7B-Instruct-GGUF",
    filename="qwen2.5-coder-7b-instruct-q4_k_m.gguf",
)
server = subprocess.Popen(
    [
        "python", "-m", "llama_cpp.server",
        "--model", gguf_path,
        "--model_alias", "qwen2.5-coder-7b-instruct",
        "--n_gpu_layers", "-1",
        "--n_ctx", "8192",
        "--host", "127.0.0.1",
        "--port", "8000",
    ],
    stdout=open("/tmp/llama_server.log", "w"),
    stderr=subprocess.STDOUT,
)
time.sleep(15)
!curl -s http://127.0.0.1:8000/v1/models
```

### Cell 3: Run the 12-task smoke test (`benchmarks/package_e2e/run_e2e.py`)
```bash
!python benchmarks/package_e2e/run_e2e.py \
  --provider custom \
  --base-url http://127.0.0.1:8000/v1 \
  --model qwen2.5-coder-7b-instruct \
  --phase functional
```

---

## Step 2 — Freeze Non-Docker Pure-Python Repo Benchmark (`Dev 40` + `Held-out 80`)

```bash
!python benchmarks/local_eval/build_dataset.py \
  --source princeton-nlp/SWE-bench_Verified \
  --out-dir benchmarks/local_eval/manifests \
  --cache-dir /content/argus_bench_cache \
  --dev-count 40 \
  --heldout-count 80 \
  --seed 42
```

---

## Step 3 — Run Experiment 1 (Baseline vs. Loop Kill-Switch + Trap-Aware Retry)

Save results to Google Drive so 90-min Colab disconnects resume cleanly:

```bash
# Mount Drive first in Colab:
# from google.colab import drive; drive.mount('/content/drive')

!python benchmarks/local_eval/run_experiment.py \
  --manifest benchmarks/local_eval/manifests/dev_40.jsonl \
  --arm baseline \
  --model qwen2.5-coder-7b-instruct \
  --base-url http://127.0.0.1:8000/v1 \
  --cache-dir /content/argus_bench_cache \
  --out /content/drive/MyDrive/argus_eval/exp1_qwen7b_baseline.jsonl

!python benchmarks/local_eval/run_experiment.py \
  --manifest benchmarks/local_eval/manifests/dev_40.jsonl \
  --arm loop_kill_retry \
  --model qwen2.5-coder-7b-instruct \
  --base-url http://127.0.0.1:8000/v1 \
  --cache-dir /content/argus_bench_cache \
  --out /content/drive/MyDrive/argus_eval/exp1_qwen7b_loop_kill_retry.jsonl

!python benchmarks/local_eval/run_experiment.py \
  --summarize \
  /content/drive/MyDrive/argus_eval/exp1_qwen7b_baseline.jsonl \
  /content/drive/MyDrive/argus_eval/exp1_qwen7b_loop_kill_retry.jsonl
```
