# Argus Repository Guidance

## Project Overview

Argus is an adaptive inference routing system.

The project is intended to make AI inference decisions based on factors such as:
- task characteristics
- model capabilities
- latency
- reliability
- cost
- observed outcomes

The system should be treated as an engineering and experimentation project, not as a generic CRUD application.

---

## Review Philosophy

Review code for real engineering problems.

Prioritize:

1. Correctness
2. Reliability
3. Safety and security
4. Reproducibility
5. Testability
6. Performance
7. Maintainability

Do not flag issues merely because you would personally implement the code differently.

Do not report speculative problems unless the diff provides a concrete mechanism that can cause them.

Prefer a small number of high-confidence findings over many low-confidence findings.

---

## Critical Areas

Pay particular attention to these parts of Argus.

### Inference Routing

Check:

- model-selection logic
- routing decisions
- fallback behavior
- provider selection
- timeout handling
- retry behavior
- failure classification
- rate-limit handling
- model availability assumptions

A routing change must not silently select an unsuitable model or provider.

---

### Recovery and Failure Handling

Argus may operate under partial failures.

Review carefully for:

- swallowed exceptions
- incorrect retry behavior
- infinite retry loops
- retry storms
- incorrect fallback ordering
- failure states that are not propagated
- inconsistent state after recovery
- timeout handling
- duplicate execution

A recovery mechanism should make the system more reliable rather than hide failures.

---

### Experimentation

Experiments are important parts of the project.

Review changes for:

- reproducibility
- deterministic configuration where appropriate
- consistent evaluation methodology
- correct baselines
- correct metric calculations
- comparable experimental conditions
- accidental leakage between experiment runs
- incorrect aggregation
- misleading conclusions caused by implementation errors

Do not assume an experiment is correct merely because it executes successfully.

---

### Telemetry and Evaluation

When code records metrics, check:

- latency measurements
- token counts
- cost calculations
- success/failure rates
- model/provider identifiers
- experiment identifiers
- timestamps
- aggregation logic

Metrics should correspond to the actual execution being measured.

---

## Python Guidelines

Prefer:

- clear type hints
- small, focused functions
- explicit error handling
- meaningful names
- dependency injection where it improves testability
- deterministic behavior in experiments

Avoid:

- broad `except Exception` blocks unless justified
- silently ignoring failures
- global mutable state when unnecessary
- hidden side effects
- duplicated routing logic

---

## Testing Expectations

New behavior should generally have relevant tests.

Tests should focus on behavior rather than implementation details.

For routing logic, test at least the important decision boundaries, such as:

- normal routing
- fallback routing
- provider failure
- model failure
- timeout
- malformed or unexpected inputs
- empty or missing data

For experiments, validate calculations against known examples.

---

## Security

Never commit:

- API keys
- access tokens
- passwords
- credentials
- private secrets
- provider authentication material

Use environment variables and secret-management mechanisms instead.

Pay special attention to:

- logging sensitive information
- returning provider credentials in errors
- persisting prompts or responses that may contain secrets
- exposing internal configuration through APIs
- unsafe dynamic URLs or endpoints

---

## Dependencies

When adding or changing dependencies, consider:

- necessity
- security
- maintenance status
- compatibility
- transitive dependencies

Avoid introducing a dependency when the existing standard library or current project dependencies are sufficient.

---

## Pull Request Review Priorities

For every PR, answer:

1. Does this change do what it claims?
2. Can it fail in realistic scenarios?
3. Does it introduce a regression?
4. Is failure/recovery handled correctly?
5. Are relevant tests present?
6. Does it affect security?
7. Does it affect experiment validity or reproducibility?
8. Does it change routing behavior in an unintended way?

If the answer to all of these is satisfactory, do not invent additional findings.

---

## Final Principle

Optimize for engineering correctness, not for the number of review comments.

A review with zero findings is valid when the code has no meaningful issues.
