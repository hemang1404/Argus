"""Generates publication-quality charts from the empirical study dataset.

Output charts saved to `figures/`:
1. `figures/cost_comparison_cascade.png` - Dual-Cost Economics & 66.6% Cascade Savings
2. `figures/accuracy_by_category.png` - Stratified 4-Quadrant Pass Rates
3. `figures/code_quality_complexity.png` - AST Cyclomatic Complexity & Maintainability
4. `figures/latency_distribution.png` - Median (P50) vs Tail (P95) Latency Profile
"""

import os
import matplotlib.pyplot as plt
import numpy as np

# Ensure figures directory exists
FIGURES_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "figures"))
os.makedirs(FIGURES_DIR, exist_ok=True)

# Set clean aesthetic styling
plt.style.use("seaborn-v0_8-whitegrid" if "seaborn-v0_8-whitegrid" in plt.style.available else "default")
plt.rcParams["font.sans-serif"] = "DejaVu Sans"
plt.rcParams["font.size"] = 11
plt.rcParams["axes.labelsize"] = 12
plt.rcParams["axes.titlesize"] = 14
plt.rcParams["xtick.labelsize"] = 10
plt.rcParams["ytick.labelsize"] = 10
plt.rcParams["legend.fontsize"] = 11
plt.rcParams["figure.titlesize"] = 16


def plot_cost_and_cascade():
    """Figure 1: Dual-Cost Economics & Production Cascade Savings."""
    fig, ax = plt.subplots(figsize=(10, 6), dpi=300)

    strategies = [
        "Direct Frontier\n(120B S0)",
        "Small Baseline\n(20B S0)",
        "Small + Few-Shot\n(20B S1)",
        "Small + RAG Docs\n(20B S2)",
        "Small + Hybrid\n(20B S3)"
    ]

    standalone_costs = [51.85, 8.29, 10.01, 11.12, 13.06]
    cascade_costs = [51.85, 14.91, 17.30, 17.31, 20.70]

    x = np.arange(len(strategies))
    width = 0.35

    rects1 = ax.bar(x - width/2, standalone_costs, width, label="Standalone Cost / 100k Solved ($)", color="#2b5c8f", edgecolor="black", alpha=0.9)
    rects2 = ax.bar(x + width/2, cascade_costs, width, label="Production Cascade Cost (with 120B Fallback) ($)", color="#00a896", edgecolor="black", alpha=0.9)

    ax.set_ylabel("Cost per 100,000 Solved Tasks ($)", fontweight="bold")
    ax.set_title("Dual-Cost Economics: Standalone Model Scaling vs. Production Cascade", fontweight="bold", pad=15)
    ax.set_xticks(x)
    ax.set_xticklabels(strategies, fontweight="bold")
    ax.legend(frameon=True, facecolor="white", edgecolor="none")
    ax.set_ylim(0, 60)

    # Add direct labels on bars
    for rect in rects1:
        h = rect.get_height()
        ax.annotate(f"${h:.2f}", xy=(rect.get_x() + rect.get_width() / 2, h), xytext=(0, 3), textcoords="offset points", ha="center", va="bottom", fontsize=9, fontweight="bold")

    for rect in rects2:
        h = rect.get_height()
        ax.annotate(f"${h:.2f}", xy=(rect.get_x() + rect.get_width() / 2, h), xytext=(0, 3), textcoords="offset points", ha="center", va="bottom", fontsize=9, fontweight="bold", color="#026b5f")

    # Add Net Savings Annotation Arrow
    ax.annotate(
        "66.6% Net Savings\nvs. Direct 120B",
        xy=(3 + width/2, 17.31), xytext=(3.2, 35),
        arrowprops=dict(facecolor="#d90429", shrink=0.08, width=2, headwidth=8),
        fontweight="bold", color="#d90429", bbox=dict(boxstyle="round,pad=0.5", facecolor="#ffebee", edgecolor="#d90429")
    )

    plt.tight_layout()
    out_path = os.path.join(FIGURES_DIR, "cost_comparison_cascade.png")
    plt.savefig(out_path)
    plt.close()
    print(f"[SUCCESS] Saved: {out_path}")


def plot_accuracy_by_category():
    """Figure 2: Stratified Pass Rates across 4 Difficulty Quadrants."""
    fig, ax = plt.subplots(figsize=(10, 6), dpi=300)

    categories = [
        "Category A\n(Easy / Control)\n[n=20 runs]",
        "Category B\n(Model Gap ⭐)\n[n=40 runs]",
        "Category C\n(Borderline / Flaky)\n[n=30 runs]",
        "Category D\n(Hard Floor)\n[n=10 runs]"
    ]

    s0_acc = [100.0, 75.0, 80.0, 30.0]
    s1_acc = [100.0, 70.0, 70.0, 50.0]
    s2_acc = [100.0, 75.0, 76.7, 40.0]
    s3_acc = [100.0, 67.5, 66.7, 40.0]

    x = np.arange(len(categories))
    width = 0.20

    ax.bar(x - 1.5*width, s0_acc, width, label="S0: Zero-Shot", color="#6c757d", edgecolor="black", alpha=0.9)
    ax.bar(x - 0.5*width, s1_acc, width, label="S1: Few-Shot (Exemplars)", color="#e76f51", edgecolor="black", alpha=0.9)
    ax.bar(x + 0.5*width, s2_acc, width, label="S2: RAG Docs (Algorithmic)", color="#2a9d8f", edgecolor="black", alpha=0.9)
    ax.bar(x + 1.5*width, s3_acc, width, label="S3: Hybrid (Docs + Ex)", color="#e9c46a", edgecolor="black", alpha=0.9)

    ax.set_ylabel("Subprocess Pass Rate (%)", fontweight="bold")
    ax.set_title("Failure Boundary Analysis across Task Difficulty Quadrants (N=400)", fontweight="bold", pad=15)
    ax.set_xticks(x)
    ax.set_xticklabels(categories, fontweight="bold")
    ax.legend(frameon=True, facecolor="white", loc="upper right")
    ax.set_ylim(0, 115)

    # Highlight Context Rescue on Category D
    ax.annotate(
        "+20% Context Rescue",
        xy=(3 - 0.5*width, 50.0), xytext=(2.6, 75),
        arrowprops=dict(facecolor="#264653", shrink=0.08, width=1.5, headwidth=6),
        fontweight="bold", color="#264653", bbox=dict(boxstyle="round,pad=0.4", facecolor="#e0f2f1", edgecolor="#264653")
    )

    plt.tight_layout()
    out_path = os.path.join(FIGURES_DIR, "accuracy_by_category.png")
    plt.savefig(out_path)
    plt.close()
    print(f"[SUCCESS] Saved: {out_path}")


def plot_code_quality():
    """Figure 3: AST Cyclomatic Complexity & Lines of Code Maintainability."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 5), dpi=300)

    models = ["Frontier 120B\n(Zero-Shot)", "Small 20B\n(S0 Zero-Shot)", "Small 20B\n(S2 RAG Docs)"]
    complexity = [2.8, 4.2, 3.1]
    loc = [8.4, 14.1, 9.6]
    colors = ["#2b5c8f", "#e63946", "#2a9d8f"]

    # Subplot 1: Cyclomatic Complexity
    bars1 = ax1.bar(models, complexity, color=colors, edgecolor="black", width=0.5, alpha=0.9)
    ax1.set_ylabel("Mean Decision Branches (Lower is Cleaner)", fontweight="bold")
    ax1.set_title("McCabe Cyclomatic Complexity", fontweight="bold")
    ax1.set_ylim(0, 5.5)
    for bar in bars1:
        h = bar.get_height()
        ax1.annotate(f"{h:.1f}", xy=(bar.get_x() + bar.get_width() / 2, h), xytext=(0, 3), textcoords="offset points", ha="center", va="bottom", fontweight="bold")

    # Subplot 2: Lines of Code (LOC)
    bars2 = ax2.bar(models, loc, color=colors, edgecolor="black", width=0.5, alpha=0.9)
    ax2.set_ylabel("Mean Lines of Code (LOC)", fontweight="bold")
    ax2.set_title("Structural Conciseness (LOC)", fontweight="bold")
    ax2.set_ylim(0, 18.0)
    for bar in bars2:
        h = bar.get_height()
        ax2.annotate(f"{h:.1f} lines", xy=(bar.get_x() + bar.get_width() / 2, h), xytext=(0, 3), textcoords="offset points", ha="center", va="bottom", fontweight="bold")

    fig.suptitle("Beyond Binary Unit Tests: AST Code Maintainability & Quality Anchor", fontweight="bold", fontsize=15, y=1.03)
    plt.tight_layout()
    out_path = os.path.join(FIGURES_DIR, "code_quality_complexity.png")
    plt.savefig(out_path)
    plt.close()
    print(f"[SUCCESS] Saved: {out_path}")


def plot_latency_distribution():
    """Figure 4: Median (P50) vs Tail (P95) Latency Profile."""
    fig, ax = plt.subplots(figsize=(9, 5), dpi=300)

    strategies = ["S0: Zero-Shot", "S1: Few-Shot", "S2: RAG Docs", "S3: Hybrid"]
    p50_latency = [2.82, 4.61, 1.62, 1.00]
    p95_latency = [8.19, 8.73, 12.35, 46.77]

    x = np.arange(len(strategies))
    width = 0.35

    rects1 = ax.bar(x - width/2, p50_latency, width, label="Median (P50) Unthrottled Inference (s)", color="#457b9d", edgecolor="black", alpha=0.9)
    rects2 = ax.bar(x + width/2, p95_latency, width, label="Tail (P95) Rate-Limit Queuing (s)", color="#e63946", edgecolor="black", alpha=0.9)

    ax.set_ylabel("Latency (Seconds)", fontweight="bold")
    ax.set_title("Inference Latency Profile: Unthrottled LPU (P50) vs. Throttled Tail (P95)", fontweight="bold", pad=15)
    ax.set_xticks(x)
    ax.set_xticklabels(strategies, fontweight="bold")
    ax.legend(frameon=True, facecolor="white")
    ax.set_ylim(0, 55)

    for rect in rects1:
        h = rect.get_height()
        ax.annotate(f"{h:.1f}s", xy=(rect.get_x() + rect.get_width() / 2, h), xytext=(0, 3), textcoords="offset points", ha="center", va="bottom", fontweight="bold")

    for rect in rects2:
        h = rect.get_height()
        ax.annotate(f"{h:.1f}s", xy=(rect.get_x() + rect.get_width() / 2, h), xytext=(0, 3), textcoords="offset points", ha="center", va="bottom", fontweight="bold", color="#a81c24")

    plt.tight_layout()
    out_path = os.path.join(FIGURES_DIR, "latency_distribution.png")
    plt.savefig(out_path)
    plt.close()
    print(f"[SUCCESS] Saved: {out_path}")


if __name__ == "__main__":
    print("[*] Generating publication-quality charts in figures/...")
    plot_cost_and_cascade()
    plot_accuracy_by_category()
    plot_code_quality()
    plot_latency_distribution()
    print("[SUCCESS] All 4 publication charts successfully generated.")
