input/tickets.csv has support tickets (id, text). Classify each ticket's category as bug, billing or cancel with the LLM at {llm_url}: an OpenAI-compatible chat completions API (model `bench-small`, any API key; OPENAI_API_KEY and OPENAI_BASE_URL are set). It answers for one ticket per request.
Write out/result.json as a JSON array with the fields id and category.
This job runs again every day on the updated file, and every LLM call is paid.
