import pytest


# Use asyncio mode for all async tests.
def pytest_configure(config):
    config.addinivalue_line(
        "markers", "asyncio: mark test as async"
    )
