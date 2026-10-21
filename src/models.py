"""Pick Gemini models this API key can actually use.

Google retires and restricts models often (e.g. new API keys get a 404 for the 2.5 family),
so instead of one hard-coded name we try a preference list, filtered by what the key can see,
and move to the next candidate whenever Google says a model is unavailable.
"""
import re
import threading

from .common import CFG, log

SKIP = ("tts", "embedding", "live", "audio", "computer-use", "robotics", "imagen", "veo",
        "lyria", "aqa", "learnlm", "gemma", "thinking-exp")
_lock = threading.Lock()
_listed: list[str] | None = None
_queues: dict[str, list[str]] = {}


def _available(client) -> list[str]:
    global _listed
    if _listed is None:
        try:
            _listed = [m.name.split("/")[-1] for m in client.models.list()
                       if "generateContent" in (m.supported_actions or [])]
            log.info("Models visible to this API key: %s", ", ".join(_listed))
        except Exception as e:  # noqa: BLE001
            log.warning("Could not list models (%s); using the configured list as-is", e)
            _listed = []
    return _listed


def _version(name: str) -> tuple[int, int]:
    m = re.search(r"gemini-(\d+)(?:\.(\d+))?", name)
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)


def _candidates(kind: str, client) -> list[str]:
    prefs = CFG["story"]["text_models"] if kind == "text" else CFG["images"]["models"]
    avail = _available(client)
    if not avail:
        return list(prefs)
    out = [m for m in prefs if m in avail]
    if kind == "text":
        auto = [m for m in avail if m.startswith("gemini-") and "image" not in m
                and "banana" not in m and not any(s in m for s in SKIP)]
        tier = lambda m: 0 if "pro" in m else 2 if "lite" in m else 1  # noqa: E731
        auto.sort(key=lambda m: (tier(m), tuple(-x for x in _version(m))))
    else:
        auto = [m for m in avail if ("image" in m or "banana" in m) and "imagen" not in m]
        # cheaper flash / nano models first; "pro" image models cost several times more
        auto.sort(key=lambda m: ("pro" in m, tuple(-x for x in _version(m))))
    return out + [m for m in auto if m not in out]


def current(kind: str, client) -> str | None:
    with _lock:
        if kind not in _queues:
            _queues[kind] = _candidates(kind, client)
            log.info("%s model candidates: %s", kind, _queues[kind])
        return _queues[kind][0] if _queues[kind] else None


def _unavailable(e: Exception) -> bool:
    msg = str(e).lower()
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    return (code == 404 or "not found" in msg or "no longer available" in msg
            or "not supported" in msg or "is not available" in msg or "deprecated" in msg
            or ("modalit" in msg and "image" in msg))


def run(kind: str, client, fn):
    """fn(model_name) with the best usable model; switches model on 'model unavailable' errors."""
    while True:
        model = current(kind, client)
        if model is None:
            raise RuntimeError(f"No usable Gemini {kind} model for this API key. "
                               f"Check the model lists in config.yaml.")
        try:
            return fn(model)
        except Exception as e:
            if not _unavailable(e):
                raise
            with _lock:
                if _queues[kind] and _queues[kind][0] == model:
                    _queues[kind].pop(0)
                    log.warning("Model %s unavailable (%s); trying %s", model, str(e)[:200],
                                _queues[kind][0] if _queues[kind] else "nothing")


def chosen() -> dict:
    return {k: (q[0] if q else None) for k, q in _queues.items()}
