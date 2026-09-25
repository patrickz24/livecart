"""Root pytest configuration, shared by every uv member's tests."""

# Integration fixtures (PostgreSQL and Redis from compose, with the _test guard).
pytest_plugins = ["livecart_core.testing.fixtures"]
