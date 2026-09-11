# Measured

Four realistic prompts, run by subagents with the skill and without it, graded against 29
assertions by an independent grader.

| | With skill | Baseline | Delta |
|---|---|---|---|
| Pass rate | **26/29** (90%) | 35% ± 15% | +0.55 |
| Time | 117s | 82s | +36s |
| Tokens | 84.5k | 77.6k | +6.9k |

The with-skill number is a single graded run against the current tree (per eval: 6/7, 6/7, 8/8,
6/7). An earlier run of the same suite, before a rewrite of the skill text and a large change to
the CLI, scored 93% ± 8% over repeated samples. The baseline column is from that earlier round
and has not been re-run, because nothing about the skill-less condition changed. Time and token
figures are also carried over from it.

The sharpest result is still a baseline failure. Asked what to be consistent with when starting a
new Telegram bot, the skill-less run read the manifests of two unrelated FastAPI services and
recommended async SQLAlchemy: precedent transferred across the wrong kind of project, stated
confidently. Another baseline searched honestly, found nothing, then asserted *"No decision
record covers it"*, which was false.

Honest limits. All three failures in the graded run are word-count assertions, over by 103, 12
and 24 words. Answers carrying real reasoning run longer than the limits allow, and the limits
have not been relaxed to make the number look better. One assertion turns on a judgment call the
grader made explicit: the run that recorded two decisions also wrote a classification tag for the
untagged project, and the grader counted that tag as metadata instead of a third journal entry.
Under a literal line count the score is 25/29. `evals/` holds the prompts, assertions and a
fixture seeder if you want to re-run or extend them.

Back to the [README](../README.md).
