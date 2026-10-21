Agent steps take `drift: {window: 7, threshold: 0.15}`: after each run the reject rate, review
rate, the share of each `str`/`bool` output value, numeric means and the cost per record are
compared with the mean of the last runs, and a change beyond the threshold emits `step.drift`
for `notify:`. `agentl stats` shows the same comparison (#155).
