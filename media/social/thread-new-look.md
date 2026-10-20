# X thread: new look + what merged

Post the thread with one card per post (cards are in `media/social/cards/`, built by `cards.py`).
Each post stays under 280 characters. Links go in the last post.

1. (card: release-new-look.png)
   agentl has a new look: blue, yellow and white.

   Same idea underneath: ETL pipelines in one YAML file, where any step can be an LLM agent with
   validated output, per-record cache and a cost budget.

   Here is what merged this week 🧵

2. (card: feature-crawl.png)
   Scraping, politely. `crawl` walks a list page and its pagination, and `html.select` turns each
   detail page into a record with CSS selectors.

   robots.txt is obeyed and requests are rate limited. Add `skip_seen` and the next run only
   processes new pages.

3. (card: feature-changeset.png)
   Writes that respect your table. `mode: changeset` compares with what is already there and
   writes only what changed, with deletes per partition and a dry run.

   Need history instead? `mode: scd2` keeps every version of a row.

4. (card: feature-outcomes.png)
   Not every run is success or failed. Runs now end as success, partial, no_change, skipped or
   failed, and you choose which ones alert and which exit code each one gets.

   An empty source is not an incident. A dead upstream is not your bug.

5. (card: feature-eval.png)
   Change prompts without fear. Give an agent step a golden set and `agentl eval` scores it, or
   compares two models side by side, before anything ships.

   Below `min_accuracy`, it exits 1. Works in CI.

6. (card: tip-cron.png)
   And the boring parts that page you at 3 a.m.: concurrency keys so runs never overlap, fetch
   windows for APIs that revise recent data, gap reports for missing days, schedules that know
   about holidays.

7. Open source, MIT, Python 3.10+.

   pip install agentl-etl

   Docs: https://agentl.com.br
   Code: https://github.com/arimayuji/agentl
