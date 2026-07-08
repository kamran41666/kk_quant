"""FastAPI application"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from server.models.database import init_db
from server.api import strategies, backtest, market, analytics, paper


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="kk_quant API",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from fastapi.responses import JSONResponse
from fastapi import Request


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import traceback
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error",
            "type": type(exc).__name__,
        },
    )


app.include_router(strategies.router, prefix="/api/v1")
app.include_router(backtest.router, prefix="/api/v1")
app.include_router(market.router, prefix="/api/v1")
app.include_router(analytics.router, prefix="/api/v1")
app.include_router(paper.router, prefix="/api/v1")


@app.get("/api/health")
def health():
    return {"status": "ok", "version": "0.2.0"}


from fastapi import WebSocket, WebSocketDisconnect
from server.ws.manager import manager


@app.websocket("/ws/{channel}")
async def websocket_endpoint(websocket: WebSocket, channel: str):
    await manager.connect(websocket, channel)
    try:
        while True:
            # Keep alive — receive client messages (pings, subscribe/unsubscribe)
            data = await websocket.receive_text()
            # Echo back for now (clients can send pings)
            if data == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        manager.disconnect(websocket, channel)
    except Exception:
        manager.disconnect(websocket, channel)
