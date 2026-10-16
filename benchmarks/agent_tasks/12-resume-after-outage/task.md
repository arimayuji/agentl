input/reviews.csv has product reviews (id, text). Get each review's sentiment (positive, negative or neutral) from the LLM at {llm_url}: an OpenAI-compatible chat completions API (model `bench-small`, any API key; OPENAI_API_KEY and OPENAI_BASE_URL are set). It answers for one review per request.
Write out/result.json as a JSON array with the fields id and sentiment. Every LLM call is paid.
