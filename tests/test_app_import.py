import asyncio

from server.main import app
from server.main import lifespan


def test_fastapi_application_imports_with_phase2_scheduler():
    assert app.title == "kk_quant API"
    assert app.version == "0.2.0"


def test_phase2_scheduler_lifespan_starts_and_stops_cleanly():
    async def exercise():
        async with lifespan(app):
            assert app.state.paper_scheduler is not None

    asyncio.run(exercise())
