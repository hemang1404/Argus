# Research status

What Argus's research claims are, what the evidence supports, and where that evidence lives.

**Where the evidence is.** The raw data, task manifests, grading logs and reports behind these claims are **not in this repository**. The research directories (`data/`, `configs/`, `results/`, `experiments_archive/`, `scripts/`, `src/`, `docs/reports/`) are gitignored and none of their files are tracked, so a fresh clone cannot reproduce any figure below. The evidence is held in the maintainer's local research workspace and has not been published. Until it is released as a versioned archive with hashes, treat every number here as maintainer-reported. This page does not link to evidence files because none are published.

**How these statuses were assigned.** They come from an internal engineering audit (November 2026) that re-computed what it could from stored files and read the code that produced the rest. Figures from the newer 100-task evaluation ("Pilot-100", write-up "Report 18") were quoted from its report and verification files, not recomputed from the per-task Docker results.

Status key: **Verified** (re-derived from raw data), **Partially** (true as a count or in a narrow scope), **Unsupported** (no adequate evidence), **Incorrect** (contradicted by the data or mis-stated).

---

## 1. What the evidence supports

Argus's patch-apply layer is a tolerant SEARCH/REPLACE matcher. In the Pilot-100 evaluation (official SWE-bench Verified Docker grading, a hashed task manifest, a gold-patch control, full FAIL_TO_PASS and PASS_TO_PASS tests, task-bootstrap intervals, preregistered predictions with misses reported):

- 99 valid development tasks, target file given to the model (so these are not leaderboard scores). All results below are for an earlier research version of the patch engine (0.2.0b1) in a separate harness, not the released 0.2.0b2.
- **gpt-oss-20b:** at temperature 0 (one run), 31 to 38 resolved with Argus applying the model's edits instead of a plain line-exact unique matcher (7 gained, 0 lost, exact McNemar p = 0.0156). Over three temperature-1.0 draws the gain was +5.7 points (95% CI 2.7-9.1). These are two separate experiments.
- **gpt-oss-120b:** +2 tasks at temperature 0 (44 to 46; p = 0.5, not significant) and +3.4 points over three draws (CI 0.7-6.7). **Qwen3.8:** about +0.3 points.
- Most of the 20B gain came from exact and indentation-tolerant matching, which other edit tools also provide. In an offline ladder of edit tools the 20B model solved 31 (exact match), 33 (substring), 37 (whitespace normalisation as in Aider) and 38 (Argus Tier 3); for the 120B model 44, 44, 46 and 46.
- The old fuzzy Tier 3 contributed one solve and one false-positive application (a one-line search matched at 0.8235 similarity in a different method; 13 other tests broke). That is why Tier 3 is now token-equal rather than similarity-based. The post-change Tier 3 has not been re-evaluated on model output.
- Tier 4 (anchors): 0 extra in a 26-task pilot and 0 discordant pairs on 261 and 419 offline candidate pools; never run on the 100-task design set. It stays opt-in.
- **Test feedback beats blind re-draws** for gpt-oss-20b on a set of 198 analysed tasks frozen before any model call: feedback minus re-draw = +6.6 points (95% CI 1.5-11.6, exact p = 0.019); gpt-oss-120b +4.5 (CI 0.0-9.1); near zero for Gemma, Qwen3.8 and Gemini (development set). One generation per task. The loop stops on the benchmark's hidden target tests, which a user would usually not have, so this is not a bound for user-visible tests. The shipped controller uses a similar feedback format.
- **Harness bug in the feedback text.** The research harness's pattern for stripping terminal colour codes lacked its escape character and deleted "[" plus a letter from test names (a test named `test_x[False]` reached the model as `test_xalse]`); 139 of 304 failure excerpts on the sealed set had unbalanced brackets. After the fix, a post hoc rerun of the affected tasks gave +5.6 points (CI 0.5-10.6, p = 0.052) for the 20B model and +3.0 (CI -1.0 to 7.6) for the 120B model. The bug did not create the effect. The development-set gpt-oss feedback arms were rerun the same way: the 20B result did not change (55 vs 47 solved for re-draws, +8 points, CI +3 to +14) and the 120B result moved from -2 to +2 points (CI -4 to +8, includes zero); Gemma, Qwen3.8 and Gemini were not rerun. The Argus package itself does not contain this bug.
- **Cheap-first cascade:** an offline replay of a 20B-with-feedback then 120B cascade solved 118 vs 108 of 198 for 120B re-draws (+5.1 points, CI 1.0-9.1) at about 1.10x spend, with up to four attempts against three. The 120B model with feedback alone solved 117, so the gain mostly reflects feedback and not routing. On the development set, two attempts of 120B alone solved more (56.7 vs 51.0 of 99) at about the same cost per solve as a test-gated cascade, with the benchmark tests as a perfect verifier. Treat the cascade as supported only as a replay. The shipped cascades are provider ladders (`CASCADE_LADDERS`), which have **not** been evaluated.
- **Cost per solve on the sealed set:** $0.0045 (20B with feedback; $0.0046 after the bug fix) vs $0.0058 (120B with re-draws), "parity at best", priced from one list-price book, Docker compute excluded.
- **Passing is not correct.** 8.3% (120B) to 12.0% (20B) of target-test passes broke other tests, and about 18% of 265 reviewed resolved patches were judged wrong or fragile. The reviews were done by AI models reading the diffs, with no human reviewer. "Resolved" means the target test and graded suite pass.
- Where Argus helps follows how often a model's edits fail to apply (a descriptive correlation of 0.73 across 8 models); for strong models the apply-layer gain is small (about +1 for Gemini).
- **Localization.** These results were obtained with the target file given. From the issue text alone, a keyword-search baseline (BM25) ranked the correct file first for 27% of development tasks and the Argus rule-based localiser for 21%. The released traceback-based localiser was not evaluated on SWE-bench, so end-to-end performance without `--file` is lower and unmeasured.

Residual weaknesses: the passing-but-wrong review was done by AI models only; the rank-correlation hypothesis (H5) was not run; one generation per task and arm; Groq free-tier rate limits cost some arms a few tasks; the cascade result is a replay; the stop rule uses hidden benchmark tests; no agentic or cross-family routing claims.

---

## 2. Claim table

| # | Claim (where it appeared) | Status | What the evidence supports instead |
| :--- | :--- | :--- | :--- |
| C1 | "Group 1B pilot: 30 paired SWE-bench Verified tasks, official tests, Docker `--network=none`" | Partially | 32 tasks were run, 26 valid (6 invalid baselines). `--network=none` was set. "Official tests" is incorrect: resolution used only the first FAIL_TO_PASS and first PASS_TO_PASS test (django-11276: 1 of 26 and 1 of 548), not the SWE-bench harness. Prefer Pilot-100. |
| C2 | "Strict baseline resolves 0 of 26" | Verified number, meaningless baseline | 23 of 26 candidates were refused by a unified-diff string check although candidates are SEARCH/REPLACE blocks. 0 is structural. Use exact SEARCH/REPLACE (Tier 1) as the baseline. |
| C3 | "Tier 1 resolves 6" | Verified (count) | On the first-F2P/P2P shortcut. |
| C4 | "Tiers 1-2-3-4 resolve 9" | Verified (count) | Tier 2 adds 3; Tiers 3 and 4 add 0 (same shortcut). |
| C5 | "McNemar p = 0.00391 vs strict" | Incorrect interpretation | The arithmetic reproduces (9 vs 0), but the comparator cannot succeed. Tier 1 vs Tier 2 is 0 vs 3, p = 0.25 (not significant). The sample is stratified on earlier outcomes. |
| C6 | "Tier 4 gives no extra benefit" | Verified, weakly | No benefit observed in small samples; opt-in; not measured on the 100-task set. Not "tested and harmful". |
| C7 | "Tier 2 rescued three tasks" | Verified with caveat | django-11066, django-11276, scikit-learn-14141; django-11276 graded on 1 of 26 F2P and 1 of 548 P2P tests. |
| C8 | "Strict vs fuzzy over 500 candidates: 26 to 50, no extra inference" | Partially; "no extra inference" Incorrect | Counts recompute, but the strict-resolved arm was derived, not executed. Candidates came from up to 4 repair attempts (23 of 50 resolutions from attempts 2-4). It is a retrospective replay, not a controlled comparison, and a plain Aider-style applier ties Argus Tiers 1-3. |
| C9 | "Execution-state routing cuts cost ~77%" | Incorrect and unsupported | "+77%" is a penalty on the prompt-only arm (1.775x, i.e. a 43.7% saving for the full state). Held-out pass rate was 93 vs 92. Costs were hard-coded estimates. |
| C10 | "About 20-feature state vector" | Partially; not what the classifier used | The 20-key function was never called; the trained model used 13 columns. No trained model is shipped. |
| C11 | "MiniLM embedding retrieval component" | Unsupported for the product | Code exists only in the archive; the package has no such dependency or component. Retrieval returned the benchmark task itself for 60 of 60 exemplars. |
| C12 | "Learned planner: CV AUC 0.97, 92/100" | Incorrect (label leakage) | A feature (`prev_pass_rate`) carried the label (0.030 vs 0.682 by outcome), was set to 0 at evaluation, and train/test sets had different distributions. No learned router is shipped; none is justified at this data size. |
| C13 | "Report 17: 92% planner / 184/200" | Incorrect | 92 of 100 was doubled. Refuted. |
| C14 | "Report 17: 63/500 (12.6%) fix rate" | Count verified, mechanism Incorrect | A 20B-to-120B cascade with apply-error feedback, tuned on the same 500, single run, no passing-but-wrong check. "Passes the (possibly truncated) harness tests", not "fix rate". |
| C15 | "Report 17: 200-400x cost reduction" | Incorrect | Compared a flat-price per-solve figure with per-attempt agent costs from other scaffolds. Four price cards give $0.29-$0.86 for the same tokens. |
| C16 | "Other Report 17 items (72, 78.5, 88, 90.5, 91.5%; 20B beats 120B; best-of-3 83%)" | Incorrect / unsupported | Not carried forward in the later report; do not cite. |
| C17 | "Zero incorrect applications" | Partially, scope-limited | True on the 500-case synthetic corpus (seven templated mutation families). One wrong-location application was observed on real model output (sympy-24539). The audit also found more wrong applications in the old Tier 3 and Tier 1; those were fixed (see CHANGELOG). |
| C18 | "Tier 3 fuzzy value" | Partially | 20B: 31 to 33 (exact plus substring) to 37 (whitespace) to 38 (Tier 3). One solve plus one false positive for the old similarity-based Tier 3. |
| C19 | Pilot-100 headline (20B 31 to 38 at temperature 0, p = 0.0156; +5.7 points over three temperature-1.0 draws) | Supported by design; not recomputed by the audit | See section 1 for limits (AI-only review, one generation per task, file given). |
| C20 | "Cheap-first / 20B-then-120B cascade is cost-effective" | Partially (replay only) | 118 vs 108 of 198 at 1.10x spend offline, but 120B with feedback alone solved 117; shipped provider ladders are unevaluated. |
| C21 | "Closed-loop test feedback rescues tasks" | Partially | Sealed set: +6.6 (20B), +4.5 (120B), +5.6 and +3.0 after the harness bug fix; about 0 for Gemma, Qwen3.8, Gemini. Uses hidden benchmark tests. Name the model. |
| C22 | "Argus localizes the buggy file" | Partially | All headline numbers had the target file given. From the issue text alone the correct file was ranked first for 27% of tasks (BM25 baseline) and 21% (Argus localiser). The traceback-based localiser was not evaluated. |
| C23 | "Tests pass means fixed" | Incorrect | About 18% of reviewed resolved patches judged wrong or fragile (AI reviewers); 8.3-12.0% of target passes broke other tests. |
| C24 | "The repository keeps its evaluation material in-tree" (old website text) | Incorrect | Research directories are gitignored and untracked; the evidence is not in a clone. |
| C25 | "Tests: 471 / 489 passing" (old README/site/changelog) | Incorrect (inconsistent, hand-copied) | See the test-count note in the CHANGELOG: one dated, environment-specific measurement (806 passed, 11 skipped, 8 deselected, 0 failed on 2026-11-16, Windows, Python 3.14). CI is the source of truth. |

---

## 3. What not to say

- "Argus doubles / improves the fix rate", "26 to 50", "200-400x cheaper", "77% cheaper", "92%", "adaptive/learned routing", "frontier-grade".
- "Official SWE-bench tests" for the 30-task pilot.
- "Zero incorrect applications" without naming the synthetic corpus.
- Any resolution-rate comparison to leaderboards: the target file was given.

## 4. How to make this reproducible

Publish a versioned research archive (task manifests with SHA-256, per-task grading logs, generation outputs, the verification scripts and the price book with its date) and link it here. Then claims C19-C22 can be re-derived by a reader.
