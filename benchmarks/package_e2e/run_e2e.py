"""End-to-end test of the Argus package through its real CLI, with a real provider.

Phases
  functional : each task in tasks.py is run with `argus run`; then approve + apply; then the original project is
               re-tested with the visible AND the hidden (stronger) tests.
  negative   : safety/refusal probes that must be refused with a specific error (most make no model call).
  repeat     : the same easy task run twice to check run-to-run reproducibility at the default temperature.

Safety rails: an isolated Argus config directory and keyring service (your real provider settings are never touched),
a global spend cap enforced from the costs Argus reports, a per-run budget, and no secret is ever printed.

  python benchmarks/package_e2e/run_e2e.py --provider gemini --cap-usd 1.0
  python benchmarks/package_e2e/run_e2e.py --only t01_off_by_one --phase functional
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
from tasks import TASKS  # noqa: E402

PY = sys.executable
ARGUS = [PY, "-c", "import sys; from argus.cli import main; main()"]
KEY_ENV = {"gemini": "GOOGLE_API_KEY", "groq": "GROQ_API_KEY", "openai": "OPENAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}


def load_dotenv_key(name: str) -> str:
    """Read one variable from the repo .env without echoing it."""
    for p in (REPO / ".env", Path.cwd() / ".env"):
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                m = re.match(rf"\s*{re.escape(name)}\s*=\s*(.*)", line)
                if m:
                    return m.group(1).strip().strip("\"'")
    return os.environ.get(name, "")


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:12]


class Harness:
    def __init__(self, args):
        self.args = args
        self.root = Path(tempfile.mkdtemp(prefix="argus_e2e_"))
        self.cfg = self.root / "cfg"
        self.cfg.mkdir()
        self.spent = 0.0
        self.results = []
        key = load_dotenv_key(KEY_ENV[args.provider])
        if not key:
            sys.exit(f"{KEY_ENV[args.provider]} not found in .env or the environment")
        self.env = dict(os.environ)
        self.env.update(
            ARGUS_CONFIG_DIR=str(self.cfg),
            ARGUS_KEYRING_SERVICE=f"argus-e2e-{os.getpid()}",
            PYTHONUTF8="1",
            PYTHONPATH=str(REPO) + os.pathsep + self.env.get("PYTHONPATH", ""),
        )
        self.env[KEY_ENV[args.provider]] = key
        self.key_secret = key

    # ---------------------------------------------------------------- helpers
    def scrub(self, text: str) -> str:
        return text.replace(self.key_secret, "<key>") if self.key_secret else text

    def argus(self, *a, cwd=None, timeout=300, inp=None):
        t0 = time.time()
        p = subprocess.run(ARGUS + list(a), cwd=cwd, env=self.env, capture_output=True, text=True, timeout=timeout, input=inp, encoding="utf-8", errors="replace")
        return dict(code=p.returncode, out=self.scrub(p.stdout), err=self.scrub(p.stderr), secs=round(time.time() - t0, 1))

    def setup_provider(self):
        a = self.args
        extra = ["--model", a.model] if a.model else []
        r = self.argus("provider", "set", "--preset", a.provider, "--api-key-env", KEY_ENV[a.provider], *extra)
        if r["code"] != 0:
            sys.exit("provider set failed:\n" + r["out"] + r["err"])
        return r

    def make_project(self, t, name=None) -> Path:
        d = self.root / (name or t["id"])
        (d / "src").mkdir(parents=True)
        (d / "tests").mkdir()
        (d / "hidden").mkdir()
        src = t["src"].replace("\n", t.get("src_eol", "\n")) if "src_eol" in t and "\r\n" not in t["src"] else t["src"]
        data = src.encode("utf-8")
        if t.get("src_bom"):
            data = b"\xef\xbb\xbf" + data
        (d / "src" / "mod.py").write_bytes(data)
        (d / "src" / "__init__.py").write_text("", encoding="utf-8")
        (d / "tests" / "test_mod.py").write_text(t["test"], encoding="utf-8")
        (d / "hidden" / "test_hidden.py").write_text(t["hidden"], encoding="utf-8")
        return d

    def pytest(self, d: Path, target: str) -> bool:
        p = subprocess.run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", target], cwd=d, capture_output=True, text=True, env=self.env)
        return p.returncode == 0

    def run_id_of(self, d: Path):
        f = d / ".argus" / "last_run_id"
        return f.read_text(encoding="utf-8").strip() if f.exists() else None

    def meta(self, d: Path, run_id):
        p = d / ".argus" / "runs" / run_id / "metadata.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def charge(self, cost):
        self.spent += float(cost or 0)
        if self.spent > self.args.cap_usd:
            self.finish(aborted=f"spend cap ${self.args.cap_usd} reached")
            sys.exit(2)

    # ---------------------------------------------------------------- phases
    def functional(self):
        a = self.args
        for t in TASKS:
            if a.only and t["id"] not in a.only:
                continue
            d = self.make_project(t)
            before = sha((d / "src" / "mod.py").read_bytes())
            vis_before = self.pytest(d, "tests")
            r = self.argus("run", "--test", f"{PY} -m pytest -q -p no:cacheprovider tests", "--file", "src/mod.py", "--provider", a.provider,
                           "--attempts", "3", "--budget", str(a.budget), "-w", str(d), *(["--model", a.model] if a.model else []))
            rid = self.run_id_of(d)
            m = self.meta(d, rid) if rid else {}
            after_run = sha((d / "src" / "mod.py").read_bytes())
            row = dict(phase="functional", id=t["id"], category=t["category"], expect=t["expect"], visible_failing_before=not vis_before,
                       exit=r["code"], secs=r["secs"], run_id=rid, status=m.get("status"), verified=m.get("verified"), tier=m.get("tier_used"),
                       attempts=m.get("attempts"), cost_usd=m.get("cost_usd"), original_untouched_after_run=(before == after_run))
            if not m:  # e.g. ALREADY_PASSING or an early refusal: take what the CLI printed
                row["cli_tail"] = (r["out"] + r["err"])[-300:]
            self.charge(m.get("cost_usd"))
            if m.get("verified") and rid:
                ap = self.argus("approve", rid, "--yes", "-w", str(d))
                ap2 = self.argus("apply", "--run-id", rid, "-w", str(d))
                row.update(approve_exit=ap["code"], apply_exit=ap2["code"], visible_after_apply=self.pytest(d, "tests"),
                           hidden_after_apply=self.pytest(d, "hidden"), file_changed_by_apply=(sha((d / "src" / "mod.py").read_bytes()) != before))
                row["verified_but_hidden_fails"] = bool(row["visible_after_apply"] and not row["hidden_after_apply"])
                raw = (d / "src" / "mod.py").read_bytes()
                if t.get("src_eol") == "\r\n":
                    row["crlf_preserved"] = (b"\r\n" in raw) and (b"\n" not in raw.replace(b"\r\n", b""))
                if t.get("src_bom"):
                    row["bom_preserved"] = raw.startswith(b"\xef\xbb\xbf")
            self.results.append(row)
            print(json.dumps(row), flush=True)

    def negative(self):
        a = self.args
        probes = []
        base = next(t for t in TASKS if t["id"] == "t02_operator")

        # N1 shell operator in the test command (no model call)
        d = self.make_project(base, "n1_shell")
        r = self.argus("run", "--test", f"{PY} -m pytest -q tests && echo pwned", "--file", "src/mod.py", "--provider", a.provider, "-w", str(d))
        probes.append(("N1 shell operator rejected", r["code"] != 0 and "pwned" not in r["out"].replace("&& echo pwned", ""), r))

        # N5 tiny budget (at most one model call)
        d = self.make_project(base, "n5_budget")
        r = self.argus("run", "--test", f"{PY} -m pytest -q tests", "--file", "src/mod.py", "--provider", a.provider, "--budget", "0.00001", "-w", str(d))
        rid = self.run_id_of(d); m = self.meta(d, rid) if rid else {}
        self.charge(m.get("cost_usd"))
        probes.append(("N5 tiny budget stops the run", r["code"] != 0 and m.get("status") != "SUCCESS", r))

        # N6 unpriced model refused before any call
        d = self.make_project(base, "n6_unpriced")
        r = self.argus("run", "--test", f"{PY} -m pytest -q tests", "--file", "src/mod.py", "--provider", a.provider, "--model", "made-up-model-9", "-w", str(d))
        probes.append(("N6 unpriced model refused", r["code"] != 0 and ("CONFIG_ERROR" in r["out"] + r["err"] or "price" in (r["out"] + r["err"]).lower()), r))

        # N2/N3/N4 need one genuine successful candidate
        d = self.make_project(base, "n_apply")
        r = self.argus("run", "--test", f"{PY} -m pytest -q tests", "--file", "src/mod.py", "--provider", a.provider, "--attempts", "3", "--budget", str(a.budget), "-w", str(d))
        rid = self.run_id_of(d); m = self.meta(d, rid) if rid else {}
        self.charge(m.get("cost_usd"))
        if m.get("verified"):
            ap = self.argus("apply", "--run-id", rid, "-w", str(d))
            probes.append(("N2 apply without approval refused", ap["code"] != 0 and (d / "src" / "mod.py").read_text(encoding="utf-8").count("> 18") == 1, ap))
            self.argus("approve", rid, "--yes", "-w", str(d))
            (d / "src" / "mod.py").write_text("# edited after the run\n" + (d / "src" / "mod.py").read_text(encoding="utf-8"), encoding="utf-8")
            ap = self.argus("apply", "--run-id", rid, "-w", str(d))
            probes.append(("N3 stale target refused", ap["code"] != 0 and "STALE_TARGET_CONFLICT" in ap["out"] + ap["err"], ap))
            mp = d / ".argus" / "runs" / rid / "metadata.json"
            data = json.loads(mp.read_text(encoding="utf-8")); data["verified"] = True; data["approved"] = True; data["attempts"] = 99
            mp.write_text(json.dumps(data), encoding="utf-8")
            ap = self.argus("apply", "--run-id", rid, "--force", "-w", str(d))
            probes.append(("N4 tampered metadata refused", ap["code"] != 0 and "SIGNATURE" in (ap["out"] + ap["err"]).upper(), ap))
        else:
            probes.append(("N2-N4 skipped: no verified candidate was produced", False, r))

        # N8 test command that hangs: the run must end by timeout, not hang
        slow = dict(base, id="n8_timeout", test="import time\nfrom src.mod import is_adult\n\n\ndef test_slow():\n    time.sleep(60)\n    assert is_adult(18) is True\n")
        d = self.make_project(slow, "n8_timeout")
        r = self.argus("run", "--test", f"{PY} -m pytest -q tests", "--file", "src/mod.py", "--provider", a.provider, "--attempts", "1", "--budget", str(a.budget), "-w", str(d), timeout=240)
        rid = self.run_id_of(d); m = self.meta(d, rid) if rid else {}
        self.charge(m.get("cost_usd"))
        probes.append(("N8 hanging test ends by timeout", r["code"] != 0 and r["secs"] < 200, r))

        for name, ok, r in probes:
            row = dict(phase="negative", probe=name, passed=bool(ok), exit=r["code"], secs=r["secs"], tail=(r["out"] + r["err"])[-200:].replace("\n", " "))
            self.results.append(row)
            print(json.dumps(row), flush=True)

    def repeat(self):
        a = self.args
        t = next(x for x in TASKS if x["id"] == "t01_off_by_one")
        hashes = []
        for i in range(2):
            d = self.make_project(t, f"rep{i}")
            self.argus("run", "--test", f"{PY} -m pytest -q -p no:cacheprovider tests", "--file", "src/mod.py", "--provider", a.provider, "--attempts", "3", "--budget", str(a.budget), "-w", str(d), *(["--model", a.model] if a.model else []))
            rid = self.run_id_of(d); m = self.meta(d, rid) if rid else {}
            self.charge(m.get("cost_usd"))
            hashes.append((m.get("status"), m.get("candidate_hash")))
        row = dict(phase="repeat", runs=hashes, identical=(hashes[0] == hashes[1] and hashes[0][1] is not None))
        self.results.append(row)
        print(json.dumps(row), flush=True)

    # ---------------------------------------------------------------- end
    def finish(self, aborted=None):
        out = Path(self.args.out or (HERE / "results"))
        out.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        f = out / f"e2e_{self.args.provider}_{stamp}.jsonl"
        with f.open("w", encoding="utf-8") as fh:
            for r in self.results:
                fh.write(json.dumps(r) + "\n")
        print(f"\nresults: {f}\nspent (as reported by Argus): ${self.spent:.4f}" + (f"\nABORTED: {aborted}" if aborted else ""))
        self.argus("provider", "delete", self.args.provider)  # remove the throwaway stored provider
        shutil.rmtree(self.root, ignore_errors=True)

    def run(self):
        self.setup_provider()
        ph = self.args.phase
        if ph in ("all", "functional"):
            self.functional()
        if ph in ("all", "negative") and not self.args.only:
            self.negative()
        if ph in ("all", "repeat") and not self.args.only:
            self.repeat()
        self.finish()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="gemini", choices=sorted(KEY_ENV))
    ap.add_argument("--model", default=None, help="pin a model (default: the preset's model)")
    ap.add_argument("--phase", default="all", choices=["all", "functional", "negative", "repeat"])
    ap.add_argument("--only", nargs="*", help="task ids (functional phase only)")
    ap.add_argument("--budget", type=float, default=0.05, help="per-run budget in USD")
    ap.add_argument("--cap-usd", type=float, default=1.0, help="global spend cap for the whole experiment")
    ap.add_argument("--out", default=None)
    Harness(ap.parse_args()).run()
