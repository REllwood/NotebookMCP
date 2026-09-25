import inspect

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if inspect.iscoroutinefunction(getattr(item, "function", None)):
            item.add_marker(pytest.mark.anyio)
