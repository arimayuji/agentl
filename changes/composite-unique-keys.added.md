`check` steps support composite unique keys: `unique: [[date, currency]]` checks the pair as one
key, and mixes with plain columns (`unique: [id, [date, currency]]`). A flat list still checks each
column on its own. The failure shows the duplicate count and a few duplicated keys, hiding values
of fields that a `protect` step marks (#348).
