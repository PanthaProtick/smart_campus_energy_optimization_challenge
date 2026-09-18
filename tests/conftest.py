def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_provider: opt-in tests that make real, potentially billable provider calls",
    )
