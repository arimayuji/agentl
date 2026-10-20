`json.read` on a JSON Lines file wraps scalar lines as `{"value": ...}`, as it already did for JSON
arrays, instead of returning non-record rows (and failing with `encoding: auto`). Found by fuzzing (#167).
