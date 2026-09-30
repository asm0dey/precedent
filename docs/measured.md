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

## Topic hygiene, 2026-09-30

Two evals were added after a real graph turned out to file decisions under project tags
(`framework` + `java`, which made `check` report Quarkus as diverging from a Telegram library).
The fixture now seeds one such decision. One graded run per condition:

| | With skill | Baseline |
|---|---|---|
| Evals 4–5 (topic hygiene) | 11/13 | 9/13 |
| All six evals | 36/43 (84%) | not re-run for 0–3 |

The baseline copied the misfiled topic into the new decision it recorded (`framework,telegram`),
and when asked to repair it, proposed keeping bare `framework`. With the skill, both runs chose
one specific topic (`bot-framework`) and proposed `amend --topic`, not a supersession.

The with-skill losses are not about topics. Three are word limits (478/450, 324/300). The rest
were one pattern: told a choice was "decided" or "settled", the run still drafted the record and
asked before writing (eval 4, and the SQLite half of eval 2), because the skill said to ask before
writing. The skill now treats an explicit settlement as the confirmation. Re-running those two
evals once, graded by the orchestrator rather than an independent grader: eval 2 went from 6/9 to
8/9 and eval 4 from 5/7 to 7/7. The remaining eval 2 miss is honest: the user gave no reason for
SQLite, so the run recorded "not yet stated" and asked for it, rather than inventing one.

Back to the [README](../README.md).
