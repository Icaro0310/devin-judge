# The eval set

`tasks.jsonl` is a small, hand-labelled set of realistic decision-task inputs
with gold answers. `source.py` is the source of truth; run it to regenerate the
JSONL:

```bash
python evalset/source.py
```

## What is in it

70 items / 203 labelled decisions across three tasks:

- **support_triage** (31 items): a customer message, labelled for `topic`
  (Choice over billing / technical / account / shipping / other), `frustration`
  (Score over low / medium / high), `is_urgent` and `wants_cancel` (Noul).
- **action_risk** (20 items): an action or command string, labelled for
  `moves_money`, `deletes_data`, `is_read_only` (Noul). The tool-gating case.
- **intent** (19 items): a short message, labelled for `intent` (Choice over
  question / complaint / praise / request).

## How it was labelled

By hand, by one person, aiming for the label a support lead would agree with.
Questions are phrased concretely on purpose: the local NLI backend answers
"this action moves money" well and "this action is dangerous" poorly, so we ask
the kind of question the model can be fairly held to. A few items are
deliberately ambiguous (mild frustration, borderline urgency) so the calibration
curve reflects real uncertainty rather than only easy cases. Ambiguous items
carry a `# ambiguous:` comment in `source.py` naming the judgment call that
was made (sarcasm, soft-delete vs destroy, request-shaped complaints).

## Honest limits

- Small: tens of items, a few hundred decisions. Good for estimating accuracy
  and calibration and for showing which way calibration moves them. Not a large
  benchmark.
- Single-labeller: no inter-annotator agreement measured. Some labels
  (frustration level, urgency) are genuinely subjective.
- English, customer-support flavoured. Not domain-general.

These limits are why the headline claim is about *calibration direction and
schema validity*, not about topping a leaderboard. The set is also the seed for
a future standalone `system-one-bench`.
