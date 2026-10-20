`agentl report --submit` files the report with your logged-in `gh` after asking, falling back to
the prefilled URL without `gh`. Reports carry a fingerprint (error class, top agentl frame,
version), and `--submit` shows existing issues with it instead of filing a duplicate. New pipeline
setting `report: {on_bug: prompt|open|off}` (default `prompt`) for the bug hint after a failed run.
(#286)
