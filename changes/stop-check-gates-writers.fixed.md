A failing check with `on_fail: stop` now stops sibling writers: every final step waits for the
pipeline's required stop checks, so `typed -> save` no longer writes while
`typed -> production -> check` fails (#278).
