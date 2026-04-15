import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import config
from rag.chain import ask, build_chain

logger = logging.getLogger(__name__)

app = FastAPI(title="Tracking App Domain Expert API", version="1.0.0")

# In-memory session store: session_id → chain instance
_sessions: dict[str, object] = {}


class AskRequest(BaseModel):
    question: str
    session_id: str = "default"


class AskResponse(BaseModel):
    answer: str
    sources: list[str]
    session_id: str


def _get_or_create_session(session_id: str) -> object:
    if session_id not in _sessions:
        logger.info("Creating new session: %s", session_id)
        _sessions[session_id] = build_chain()
    return _sessions[session_id]


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/ask", response_model=AskResponse)
def ask_expert(request: AskRequest) -> AskResponse:
    try:
        chain = _get_or_create_session(request.session_id)
        result = ask(request.question, chain)
        return AskResponse(
            answer=result["answer"],
            sources=result["sources"],
            session_id=request.session_id,
        )
    except Exception as e:
        logger.exception("Error processing question: %r", request.question)
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/sessions/{session_id}")
def clear_session(session_id: str) -> dict:
    _sessions.pop(session_id, None)
    return {"status": "cleared", "session_id": session_id}
