"""Use cases and their ports: the layer that drives the domain.

It may import the standard library, pydantic and ``livecart_core.domain``
only. The ports are ``typing.Protocol`` classes, and the infrastructure
adapters that satisfy them are injected by the entry points, so a use case is
tested with fakes and no I/O. The ports and use cases arrive in the next
commits of milestone 2; tests/architecture/test_layering.py checks the rule.
"""
