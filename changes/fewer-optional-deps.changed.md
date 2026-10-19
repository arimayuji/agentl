The `google` extra no longer installs `requests`: the service account token refresh uses a
standard-library transport. Fuzzy matching in `resolve` is tested on both backends (`rapidfuzz`
and the `difflib` fallback), and CI runs the test suite with only the core dependencies (#294).
