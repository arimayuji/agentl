`agentl mcp` serves MCP resources (docs, the pipeline schema, templates, connectors, pipeline
files under `--root` and run summaries) and prompts (build a pipeline, explain the last failed
run, estimate the cost), and a streamable HTTP transport with `agentl mcp --http :7072`: localhost
by default, with an optional bearer token from `AGENTL_MCP_TOKEN` (#165).
