"""The patient chat (iteration 3). Your code runs these steps in order; the LLM is used in steps 1, 2 and 4:

  1. red-flag check on the patient's own message   -> a red flag stops here (emergency + urgent case, no answer)
  2. rewrite the follow-up into a standalone question
  3. hybrid search (BM25 + vector), top 4 chunks
  4. answer from those chunks only (answer, source_ids, answered)
  5. code adds the Sources line, or the fixed "I don't know"

The chat can't see slots or book yet (iteration 4). Nothing from the chat is saved, except urgent cases.
"""
import sqlite3

from . import llm, rag, service
from .rules import Refusal

MAX_MESSAGE = 500
MAX_HISTORY = 6
CHECK_FAILED = "We couldn't check your message just now. Please try again, or call the clinic if it's urgent."


def clean_history(history) -> list[dict]:
    """Keep only the last 6 well-formed messages (the browser sends them; nothing is stored on the server)."""
    out = []
    for m in history or []:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            out.append({"role": role, "content": content.strip()[:MAX_MESSAGE * 2]})
    return out[-MAX_HISTORY:]


def handle_message(conn: sqlite3.Connection, patient_id: str, message: str, history) -> dict:
    message = " ".join((message or "").split())
    if not message:
        raise Refusal("MESSAGE_REQUIRED", "Please type a message.", http_status=422)
    if len(message) > MAX_MESSAGE:
        raise Refusal("MESSAGE_TOO_LONG", f"Please keep your message to {MAX_MESSAGE} characters or fewer.", http_status=422)
    history = clean_history(history)

    # 1. red flag, before anything else. If the check cannot run, do not answer.
    try:
        flag = llm.check_red_flags(message)
    except llm.RedFlagCheckError:
        return {"kind": "check_failed", "reply": CHECK_FAILED, "sources": [], "emergency": None}
    if flag.red_flag:
        urgent_id = service.save_urgent_case(conn, patient_id, message, flag.category, flag.why, source="chat")
        return {"kind": "emergency", "reply": llm.EMERGENCY_RESPONSE, "sources": [],
                "emergency": {"message": llm.EMERGENCY_RESPONSE, "category": flag.category, "urgent_id": urgent_id}}

    try:
        question = rag.rewrite_question(message, history)                      # 2. rewrite
        rows = rag.search_hybrid(question)                                      # 3. hybrid search
        age = service.patient_history(conn, patient_id)["patient"]["age"]
        result = rag.answer_question(question, rows, age)                       # 4. answer; 5. sources / "I don't know" by code
    except Exception:
        return {"kind": "error", "reply": rag.CHAT_ERROR, "sources": [], "emergency": None}

    return {"kind": result["kind"], "reply": result["answer"], "sources": result["sources"],
            "emergency": None, "rewritten": question}
