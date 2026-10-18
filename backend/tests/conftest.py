import os
from urllib.parse import urlparse

import pytest

url = os.getenv("TEST_DATABASE_URL")
if not url:
    raise RuntimeError("Tests require TEST_DATABASE_URL; refusing to use DATABASE_URL")
if "test" not in urlparse(url).path.lower():
    raise RuntimeError("TEST_DATABASE_URL database name must contain 'test'")
os.environ["DATABASE_URL"] = url


@pytest.fixture(autouse=True)
def isolated_schema():
    from citko import models  # noqa: F401
    from citko.db import Base, engine

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)
