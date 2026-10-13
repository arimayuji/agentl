"""Thin adapters for orchestrators. Each module imports its orchestrator lazily, so
installing agentl never pulls Celery, Temporal or Airflow in."""
