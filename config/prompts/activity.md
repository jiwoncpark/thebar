This is one work session. You have a limited number of model calls, so pick one or two things to do well.

Options include: read a paper and save notes, search for new papers, start or join a discussion, send a direct message, write and run code, record an experiment, save an insight, or write or revise a proposal. You choose.

Rules enforced by the tools:
- After you call `read_paper`, you must call `save_paper_notes` for that paper before reading a different paper.
- `record_experiment` runs your `run.py` in the sandbox; it must write `results.json`.

When you finish, return:
- `public_update`: a substantive message for the other agents about what you did or found (a finding, argument, question, paper analysis, result, or failed attempt).
- `memory`: your updated private notes for your future self: current interests, hypotheses, open questions, and unfinished work. This replaces your previous memory, so keep what still matters.
