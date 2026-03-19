import pytest
import asyncio
import server

# Fix for pytest-asyncio async fixtures
@pytest.fixture(scope="session")
def event_loop():
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
    yield loop
    loop.close()

# The proper modern pytest-asyncio way is to use async def with @pytest_asyncio.fixture or pytest.mark.asyncio around tests
@pytest.fixture(scope="session", autouse=True)
def setup_server_data(event_loop):
    event_loop.run_until_complete(server.fetch_israel_cities())

def test_extract_area_prefixes():    
    # Test prefixes
    prefixes = ['ל', 'ב', 'מ', 'ה']
    for p in prefixes:
        text = f"אזעקה {p}ירושלים ו-{p}חיפה" # We use space or dash in real life typically
        areas = server.extract_areas_from_text(text)
        assert 'ירושלים' in areas
        assert 'חיפה' in areas
