# Evaluation definitions

> **Historical document, shortened.** It once described an early evaluation: a 500-case synthetic patch corpus and a single 50-task run on one Gemini model through the Antigravity host (HumanEval/MBPP-style tasks, one run, $0.00 metered spend). That 50-task run could not rank strategies (very different routers scored 92, 89, 93 and 93 of 100), showed no SWE-bench resolution, and its raw data is not in this repository, so its token, latency and pass-rate tables have been removed. This page keeps only the definitions that other documents rely on. The current, audited statement of what the evidence supports is [RESEARCH_STATUS.md](RESEARCH_STATUS.md). "Resolved" in the newer SWE-bench work means the target test and graded suite pass, not that the patch is correct.

---

## 1. The synthetic patch corpus

`tests/test_patch_adversarial_corpus.py` contains 500 synthetic cases built from seven templated mutation families (whitespace, indentation, blank lines, duplicate matches, anchor conflicts, multi-file targets and similar). It tests patch-engine precision and safe refusal. It is **not** a sample of real model output: one wrong-location application by the earlier fuzzy Tier 3 was observed on real model output even though this corpus showed zero incorrect applications.

## 2. Four-way classification of a patch application

Incorrect application is worse than refusal, because it silently corrupts the working tree. Each application is classified into exactly one category:

| Classification | Definition |
| :--- | :--- |
| Correct recovery | The patch was applied to the intended region and the result matches the expected output. |
| Safe refusal | The patch was ambiguous, multi-file or unmatchable and was rejected without changing the file (`AMBIGUOUS_MATCH`, `MULTI_FILE_UNSUPPORTED`, `NO_MATCH_FOUND`). |
| Failed recovery | The patch applied or was refused, but the tests still failed within the attempt budget. |
| Incorrect application | The patch was applied to the wrong place, with mismatched content, or with mangled indentation or syntax. The goal is zero. |

On the synthetic corpus, the check compares the result with the expected output line by line, ignoring blank lines and trailing whitespace, but not indentation.

## 3. What syntax validation does and does not do

Argus checks candidates with `ast.parse` and `compile`. This confirms that the file is valid Python syntax. It does not check types, scopes, runtime behaviour or correctness, and it is not a security sandbox.

## 4. Statistics used in the research evaluations

Comparisons are paired by task: for each task, the outcome under one strategy minus the outcome under another. Uncertainty is a 95% percentile interval from a bootstrap that resamples tasks, together with an exact McNemar test on the tasks where the two strategies disagree. These intervals describe which tasks were drawn. They do not describe the run-to-run randomness of the model, which was not measured separately. See [RESEARCH_STATUS.md](RESEARCH_STATUS.md) for the results and their limits.

## 5. Threats to validity

1. **Synthetic versus real drift.** Real model patches can combine kinds of drift that the corpus does not cover.
2. **Contamination.** HumanEval, MBPP and SWE-bench tasks are public, so models may have seen them.
3. **Model specificity.** Results for one provider or model family may not hold for another.
4. **Sampling variance.** Resampling results depend on the sampling temperature.
5. **Single-file scope.** The patch engine handles single-file Python targets. Multi-file edits are refused.
