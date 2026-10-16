GET {api_url}/events returns order events as a JSON array (id, status, updated_at). GET {api_url}/events?since=2026-10-01T10:00:00 returns only the events with updated_at after that time.
Load the events into the SQLite database out/events.db, table events (columns id, status, updated_at; one row per id, the latest state).
This job runs every hour, and the API bills per record it returns.
