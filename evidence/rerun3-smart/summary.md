| signature | outcome | revert test (before -> after) | smart vs plain (eval rounds) |
|---|---|---|---|
| call_args:issue_refund.amount | culprit b0be626692 (confidence medium) | PASSED: pass 0% -> 100% | 24 plain vs 18 smart |
| must_not_call | culprit 0ecffc0942 (confidence high) | PASSED: pass 0% -> 100% | 4 plain vs 3 smart |
| budget:tokens@refund_outside_policy | culprit 5cee99b624 (confidence medium) | inconclusive | 21 plain vs 21 smart |
| call_args:escalate_to_human.priority | known pre-existing failure — correctly refused | not run | n/a (no compare data) |
| budget:tokens@refund_within_policy | culprit 59c8b10ccb (confidence low) | FAILED: median tokens 39,306 -> 35,195, within budget 0/3 -> 0/3 | 54 plain vs 60 smart |
| budget:tokens@order_status | culprit 59c8b10ccb (confidence low) | FAILED: median tokens 20,405 -> 22,861, within budget 0/4 -> 0/4 | 84 plain vs 96 smart |
