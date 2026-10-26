"""
Test suite for UBEM-MCP.

- test_batch_manager.py: batch orchestration logic, with the worker mocked
  (fast, no EnergyPlus needed)
- test_integration_energyplus.py: real EnergyPlus simulations through the
  batch manager's worker processes (marker ``energyplus``; skipped when
  EnergyPlus is not installed)
- test_auth.py, test_config_transport.py: streamable-HTTP transport and auth

Run only the fast tests with ``pytest -m "not energyplus"``.
"""

__version__ = "1.0.0"
