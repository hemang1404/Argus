# Argus Documentation & Research Compendium

This directory serves as the centralized repository for all Argus academic documents, technical reports, presentation materials, and research roadmaps.

---

## Directory Structure

```
docs/
├── synopsis/                    # Official Minor Project Synopsis files & templates
│   ├── Minor_Project_Synopsis_Argus_v3.docx   ⭐ Final corrected synopsis (Proposal framing)
│   ├── Minor_Project_Synopsis_Argus_v3.pdf    ⭐ Exported PDF version
│   ├── Minor_Project_Synopsis_Argus_v2.docx   # Previous revision
│   ├── Minor_Project_Synopsis_Argus_MSP10.*   # Format verification drafts
│   ├── MSP 10.docx, MSP 9.docx                # Institutional reference templates
│   └── PROJECT_SYNOPSIS.md                    # Markdown reference text
│
├── reports/                     # Comprehensive research reports, evaluation data & compendiums
│   ├── Argus_Full_Research_Compendium_All_Phases.docx ⭐ Complete 4-phase research compendium
│   ├── Argus_Full_Research_Compendium_All_Phases.pdf  ⭐ Printable PDF of full compendium
│   ├── ALL_EXPERIMENTS_MASTER_CATALOG.*              # Master catalog of all experimental runs
│   ├── Report_SWEBench_300_Final.*                   # 300-task SWE-bench full evaluation report
│   ├── Report7_Review_and_Data_Audit.*               # 10-point data audit & statistical review
│   ├── Adaptive_Inference_Planning_Mentor_Report.*   # Mentor summary report
│   ├── argus_experiments_and_significance.*          # Statistical significance & effect size notes
│   └── PROGRESS_REPORT.*                             # Tracked milestone progress report
│
├── presentations_and_guides/   # Presentation slides, defense Q&A & deep dives
│   ├── ARGUS_MASTER_GUIDE.*                          # System master guide & architectural handbook
│   ├── DEFENSE_QNA.*                                 # Oral defense questions & answers prep
│   ├── SLIDE_DECK.md                                 # Presentation slide deck outline
│   ├── STUDY_GUIDE.md                                # Comprehensive exam & viva study guide
│   ├── llm_deep_dive.*                               # Model capabilities & routing deep dive
│   └── system_deep_dive.*                            # Execution harness & sandbox architecture
│
└── planning/                    # Project roadmaps & implementation plans
    ├── argus_research_milestone_and_roadmap.md       # Research phases & deliverables roadmap
    ├── DOCKER_EVALUATION_PLAN.md                     # Sandbox containerization evaluation plan
    └── implementation_plan*.md                       # Development sprint plans
```

---

## Primary Documents Quick Links

| Document | Format | Description |
| :--- | :--- | :--- |
| **Minor Project Synopsis (v3)** | [DOCX](file:///c:/coding/Argus/docs/synopsis/Minor_Project_Synopsis_Argus_v3.docx) \| [PDF](file:///c:/coding/Argus/docs/synopsis/Minor_Project_Synopsis_Argus_v3.pdf) | Official institutional proposal framing Argus as a general-purpose adaptive inference orchestration framework. |
| **Full Research Compendium** | [DOCX](file:///c:/coding/Argus/docs/reports/Argus_Full_Research_Compendium_All_Phases.docx) \| [PDF](file:///c:/coding/Argus/docs/reports/Argus_Full_Research_Compendium_All_Phases.pdf) | Complete 4-phase academic write-up detailing theoretical foundations, benchmark evaluations, SWE-bench validation, and statistical significance. |
| **SWE-Bench 300 Report** | [DOCX](file:///c:/coding/Argus/docs/reports/Report_SWEBench_300_Final_Evaluation.docx) \| [PDF](file:///c:/coding/Argus/docs/reports/Report_SWEBench_300_Final.pdf) | Rigorous evaluation across 300 real-world repository tasks with Docker sandbox verification. |
| **Oral Defense Q&A** | [MD](file:///c:/coding/Argus/docs/presentations_and_guides/DEFENSE_QNA.md) \| [PDF](file:///c:/coding/Argus/docs/presentations_and_guides/DEFENSE_QNA.pdf) | Anticipated questions, theoretical justifications, and defense responses. |
| **System Master Guide** | [MD](file:///c:/coding/Argus/docs/presentations_and_guides/ARGUS_MASTER_GUIDE.md) \| [PDF](file:///c:/coding/Argus/docs/presentations_and_guides/ARGUS_MASTER_GUIDE.pdf) | Complete developer handbook for configuring routing policies, execution harnesses, and models. |

---

## Rebuilding Documents

All automated document generation scripts are kept in [`tools/doc_builders/`](file:///c:/coding/Argus/tools/doc_builders/). See [`tools/doc_builders/README.md`](file:///c:/coding/Argus/tools/doc_builders/README.md) for generation commands.
