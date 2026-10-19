The run store records a schema version and applies ordered migrations on open, on SQLite and
Postgres, replacing the ad hoc column checks; stores from older releases upgrade in place and keep
their cache, state, seen keys and runs. CI gains a `lowest` job that tests the core at its lowest
declared dependency versions on Python 3.10 (#340).
