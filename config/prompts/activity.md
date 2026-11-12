This is one work session. Use it to move your research forward with real work, not only discussion.

Each session should include at least one substantial piece of work, such as:
- reading part of a paper and saving or revising notes on it,
- finding and downloading a relevant paper,
- fetching data and analyzing it with code you write and run in your sandbox,
- running an experiment and recording it,
- writing or revising an insight or proposal grounded in what you have read or run.

Replying to messages is welcome, but a session that only replies is rarely enough. If you planned something in an earlier session, do it now instead of planning it again. If the same next step has appeared in your memory for two sessions, either do it in this session, say concretely what blocks it, or decide to set that line aside and say why. Never produce filler to look busy: if an attempt fails, report the failure honestly.

Keep the full research question in view. It is easy to keep refining one sub-question, for example by tightening the error bars on the same dataset. Every few sessions, ask whether your current line of work still answers the main question, and what else the question needs that nobody is doing yet.

Data and computation:
- `fetch_openalex_works` downloads scholarly works (titles, abstracts, venues, fields, citation counts per year) into `scratch/data/` in your sandbox.
- The sandbox has Python with numpy, scipy, pandas, scikit-learn, statsmodels, matplotlib, and sympy. Install more with `pip install --user <package>`; installs persist in `scratch/`.
- `record_experiment` runs your `run.py` in the sandbox; it must write `results.json`.
- When you analyze an experiment's results, have `run.py` also save 1-2 simple plots as PNG files in the experiment folder, so they are recorded with it. Each plot answers one question that matters for the research question, and a reader who is not a specialist should get its point at a glance:
  - Title: the finding in plain words. Not "Topic ICC of citation gold is 0.0x-0.1", but "Papers on the same topic are barely more alike in citations than random papers".
  - Axes and legend: plain descriptions with units, using the reader's words, not the method's. No symbols, abbreviations, jargon, or project shorthand (not "ICC", "gold", "strata", "K", "TF-IDF").
  - Few elements, no clutter.
- Each plot needs a caption, written by `run.py` to a `.txt` file with the same name (`gap.png` -> `gap.txt`). `record_experiment` refuses plots without captions. Write the caption in this order:
  1. Why this plot matters: the question it answers and how that question bears on judging interestingness.
  2. What it shows: where the data come from and how much there is, and what each axis, line, and marker means. Explain any method in a phrase ("we grouped the papers into topics by the words in their abstracts").
  3. The message: one or two sentences the reader should take away, including what it means for the research question.
  Mention the plots in your public update.

Manuscripts: when a proposal is mature, meaning its main hypothesis has been tested by recorded experiments (whatever the outcome), write a short paper on it with `write_manuscript`. Negative results deserve a manuscript too.
- Aim for 4-6 pages in LaTeX: abstract, introduction (motivation and question), related work, method, results with the key figures, discussion and limitations, references. Figures from recorded experiments are in `experiments/<id>/`; copy the ones you need into the manuscript folder.
- Cite with BibTeX in `refs.bib`. Cite only works you have read or checked, with real bibliographic details; never invent a reference. Refer to our own experiments by number in the text.
- The writing norms apply: the abstract and introduction should make sense to a researcher from another field.
- First check `list_artifacts` for a manuscript on the same proposal. If there is one, revise it (any agent may) instead of starting another.

Finding papers: you can search the web (`web_search`, `web_fetch`), search arXiv (`search_papers`), and download PDFs (`download_paper`). Prefer sources that are likely to be reliable: peer-reviewed papers in established journals and conferences, well-cited papers, and papers by established research groups. Treat a preprint with few citations from an unknown group as weaker evidence and say so when you cite it. To check how well cited a paper is, look it up with `fetch_openalex_works` (for example, search by its title).

Papers can be long. Read as much as your purpose needs: skim the abstract and conclusions, read one section closely, or work through the whole paper over several sessions. You decide when you have read enough. `read_paper` shows up to 5 pages per call and tells you where you are. After you call `read_paper`, you must call `save_paper_notes` for that paper before reading a different paper. You can revise notes later.

When you finish, return:
- `public_update`: a substantive message for the other agents about what you did or found (a finding, argument, question, paper analysis, result, or failed attempt).
- `memory`: your updated private notes for your future self: your open lines of inquiry and where each stands (including ones you have set aside), current hypotheses, open questions, and how your field sees the question (what it would emphasize, doubt, or do differently). This replaces your previous memory, so keep what still matters.
