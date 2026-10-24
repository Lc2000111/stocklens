"""Usage log and optional spending limits for the AI features.

  1. Usage log (always on)  every AI request is recorded in the database (table ai_usage): which agent, how many
                         LLM calls, input / cached / output / reasoning tokens, time taken, success or failure.
                         This is also where the report's measured token counts come from.
  2. Optional limits (OFF by default; this is a class assignment). Set any of these environment variables
     to a positive number to turn a limit on, e.g. on Render if the link is ever shared widely:
       AI_CALLS_PER_HOUR    requests per visitor per rolling hour
       AI_DAILY_CALL_CAP    requests per day, site-wide
       AI_DAILY_TOKEN_CAP   tokens per day, site-wide
     Over-limit requests get HTTP 429 with a friendly message.

Visitors are identified by a salted SHA-256 hash of their IP address, so raw IPs are never stored.
A request is "reserved" before the AI runs, so a burst of clicks can't slip past the limits.
Limits can be changed with environment variables (e.g. on Render) without editing code.
"""

import hashlib
import os
import threading
import time

import db

PER_VISITOR_PER_HOUR = int(os.environ.get("AI_CALLS_PER_HOUR", 0))   # 0 = no limit
DAILY_CALL_CAP = int(os.environ.get("AI_DAILY_CALL_CAP", 0))
DAILY_TOKEN_CAP = int(os.environ.get("AI_DAILY_TOKEN_CAP", 0))
SALT = os.environ.get("AI_GUARD_SALT", "stocklens-mgt409")

_lock = threading.Lock()


def visitor_id(request):
    """Stable, anonymous id for the person making the request."""
    ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()  # real client IP behind Render's proxy
          or request.remote_addr or "unknown")
    return hashlib.sha256(f"{SALT}:{ip}".encode()).hexdigest()[:16]


class LimitReached(Exception):
    """Raised when a request would go over a limit; the message is shown to the visitor."""


def reserve(visitor, agent):
    """Check any limits that are switched on and record the request as started. Returns the usage-row id."""
    with _lock:
        s = db.ai_usage_summary(visitor)
        if PER_VISITOR_PER_HOUR and s["visitor_last_hour"] >= PER_VISITOR_PER_HOUR:
            raise LimitReached(f"You've used {s['visitor_last_hour']} AI requests in the last hour "
                               f"(limit {PER_VISITOR_PER_HOUR}). Please try again a little later.")
        if DAILY_CALL_CAP and s["calls_today"] >= DAILY_CALL_CAP:
            raise LimitReached(f"The site has reached today's limit of {DAILY_CALL_CAP} AI requests. "
                               "AI features will be back tomorrow; everything else still works.")
        if DAILY_TOKEN_CAP and s["tokens_today"] >= DAILY_TOKEN_CAP:
            raise LimitReached(f"The site has used today's AI budget ({DAILY_TOKEN_CAP:,} tokens). "
                               "AI features will be back tomorrow; everything else still works.")
        return db.ai_usage_start(visitor, agent)


def finish(row_id, result=None, error=None, started=None):
    """Record the outcome and token usage of a request reserved with reserve()."""
    usage = (result or {}).get("usage") or {}
    db.ai_usage_finish(row_id, ok=error is None, model=(result or {}).get("model"),
                       llm_calls=usage.get("llm_calls", 0), input_tokens=usage.get("input_tokens", 0),
                       cached_tokens=usage.get("cached_tokens", 0), output_tokens=usage.get("output_tokens", 0),
                       reasoning_tokens=usage.get("reasoning_tokens", 0),
                       ms=int((time.time() - started) * 1000) if started else None,
                       detail=(str(error)[:200] if error else None))


def status(visitor):
    """Current usage vs limits (shown on the Ask page)."""
    s = db.ai_usage_summary(visitor)
    return {**s, "limits": {"per_visitor_per_hour": PER_VISITOR_PER_HOUR, "daily_calls": DAILY_CALL_CAP,
                            "daily_tokens": DAILY_TOKEN_CAP}}
