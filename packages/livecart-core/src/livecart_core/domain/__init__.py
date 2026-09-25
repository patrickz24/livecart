"""Pure business rules: value objects, entities, the parser and the state machines.

This layer imports the standard library and itself only: never pydantic,
SQLAlchemy, Redis, FastAPI, structlog or the settings. Every rule can then be
tested without a database, and a bound such as the largest quantity per comment
reaches it as a plain argument. tests/architecture/test_layering.py reads the
imports of every module here and fails on any other.
"""
