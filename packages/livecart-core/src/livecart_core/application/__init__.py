"""Use cases and their ports: the layer that drives the domain.

It may import the standard library, pydantic and ``livecart_core.domain``
only. ``ports`` holds the ``typing.Protocol`` classes and ``dto`` the data
they carry; the infrastructure adapters that satisfy them are injected by the
entry points, so a use case is tested with the fakes of
``livecart_core.testing.fakes`` and no I/O. The use cases arrive with the
milestones that need them; tests/architecture/test_layering.py checks the
import rule.
"""
