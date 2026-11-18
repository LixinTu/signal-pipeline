# Sample output

Generated tables stay out of this repository (see Limits in the main README), so these two files show what the pipeline writes.

| File | What it is |
|---|---|
| `funnel.csv` | **Real counts** from the run of 2026-11-17 on the author's machine: where each of the 40 companies with a relevant posting went. Counts only. |
| `accounts_SIMULATED.csv` | **SIMULATED.** The first 5 rows of `accounts.csv` as the offline checks produce it from made-up companies with `.example` domains. It has the columns and the wording of the real file, and no real company. `python tests/check_build_accounts.py --write-sample` rewrites it, and the checks fail if it drifts from what the script writes. |

The weekly run's counts are in [`../weekly_runs.csv`](../weekly_runs.csv), one row per run.
