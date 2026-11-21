# Evidence

- Unit tests: `python3 -m unittest discover -s tests` passes 80 tests on Python 3.12 (Linux) and 3.9 (macOS), stdlib only, also with `-W error::ResourceWarning`.
- `hermes plugins validate .` passes with no warnings; `hermes plugins doctor .` registers 4 hooks.
- Regression tests cover: a held store lock never causing a host timeout veto of unrelated tools; denials attributed only by exact call ID (interleaved proposals, unknown IDs, reused IDs while comparison is busy); origin scoping per chat; Hermes-compatible job lookup (case, exact ID, ambiguity); a bounded store that stays enforceable as history grows; `/declines clear` fenced against writes already queued; recognition of Hermes' recurring Slack schedule forms with bounded parsing.
- Each regression test was checked to fail against the code it fixes.
- Independent code review over several rounds; the final round found no P1/P2 issues.
- Live smoke on stock Hermes v0.23.3: `cronjob_manage` does not prompt there, so nothing is recorded (expected); the existing-duplicate veto works.
- Live smoke against PR #158289 core: see README, "Native approval verification".
