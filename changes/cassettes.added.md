`agentl run --record DIR` keeps the run's HTTP responses and LLM answers in a cassette with secrets
hidden, `agentl test --replay DIR` runs offline from it, and `agentl eval add ... --from-cassette DIR`
turns the recorded answers into golden lines (#211).
