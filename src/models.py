"""Pick Gemini models this API key can actually use.

Google retires and restricts models often (e.g. new API keys get a 404 for the 2.5 family),
so instead of one hard-coded name we try a preference list, filtered by what the key can see,
and move to the next candidate whenever Google says a model is unavailable.
"""
import re
import threading

from .common import CFG, gh_annotate, log

SKIP = ("tts", "embedding", "live", "audio", "computer-use", "robotics", "imagen", "veo",
        "lyria", "aqa", "learnlm", "gemma", "thinking-exp")
TEXT_RE = re.compile(r"^gemini-(?:\d+(?:\.\d+)?-)?(?:pro|flash|flash-lite)(?:-preview)?"
                     r"(?:-\d{2}-\d{2,4})*(?:-latest)?$")
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
        # only general chat models (pro / flash / flash-lite), never transcribe/tts/tools variants
        auto = [m for m in avail if TEXT_RE.match(m)]
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


_free_tier_blocked: set[str] = set()

FREE_TIER_HELP = (
    "Your Gemini API key is on the FREE tier, which has no quota for these models "
    "(image generation is never free). Turn on billing for the key's Google Cloud project: "
    "aistudio.google.com/apikey -> your key -> 'Set up billing' (free-trial credits, if your "
    "Google Cloud account has them, are used first). Then run '1 Prepare story + images' again.")


def _zero_quota(msg: str) -> bool:
    """429 with 'limit: 0' = this key has no allowance at all for the model (e.g. free tier)."""
    return ("resource_exhausted" in msg or "429" in msg) and "limit: 0" in msg


def _unavailable(e: Exception) -> bool:
    msg = str(e).lower()
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    return (code == 404 or "not found" in msg or "no longer available" in msg
            or "not supported" in msg or "is not available" in msg or "deprecated" in msg
            or ("modalit" in msg and "image" in msg) or "not enabled for this model" in msg
            or _zero_quota(msg))


def _busy(e: Exception) -> bool:
    """Temporary overload (503 'high demand', 500, 'overloaded'): try another model, come back later."""
    msg = str(e).lower()
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    return (code in (500, 503) or "high demand" in msg or "overloaded" in msg
            or "503" in msg or "'status': 'unavailable'" in msg or "try again later" in msg)


def run(kind: str, client, fn):
    """fn(model_name) with the best usable model; switches model on 'model unavailable' errors
    and rotates past models that are temporarily overloaded."""
    busy: set[str] = set()
    while True:
        model = current(kind, client)
        if model is None:
            if _free_tier_blocked:
                raise RuntimeError(FREE_TIER_HELP)
            raise RuntimeError(f"No usable Gemini {kind} model for this API key. "
                               f"Check the model lists in config.yaml.")
        try:
            return fn(model)
        except Exception as e:
            if _busy(e) and not _unavailable(e):
                with _lock:
                    busy.add(model)
                    q = _queues[kind]
                    if set(q) <= busy:  # every candidate is busy right now: let the caller wait
                        raise
                    if q and q[0] == model:
                        q.append(q.pop(0))  # keep it, but try the others first
                log.warning("Model %s busy (%s); trying %s", model, str(e)[:120], _queues[kind][0])
                gh_annotate("notice", f"{kind} model {model} busy", str(e)[:400])
                continue
            if not _unavailable(e):
                raise
            if "free_tier" in str(e).lower():
                _free_tier_blocked.add(model)
            with _lock:
                if _queues[kind] and _queues[kind][0] == model:
                    _queues[kind].pop(0)
                    log.warning("Model %s unavailable (%s); trying %s", model, str(e)[:200],
                                _queues[kind][0] if _queues[kind] else "nothing")
                    gh_annotate("notice", f"{kind} model {model} skipped", str(e)[:600])


def chosen() -> dict:
    return {k: (q[0] if q else None) for k, q in _queues.items()}
