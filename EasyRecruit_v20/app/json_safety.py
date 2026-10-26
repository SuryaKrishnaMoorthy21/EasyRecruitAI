"""
Non-finite numbers must never reach a JSON response or a stored JSON document (Task 7).

JSON has no NaN or Infinity. Python's `json.dumps` writes the literals `NaN` / `Infinity` by default, which
browsers' `JSON.parse` and most clients reject, while Starlette's `JSONResponse` refuses them
(`allow_nan=False`) and the request dies as an unhandled HTTP 500. Either way a single stray `nan` anywhere in
a result (a failed division, an empty embedding, a bad model output) used to take the whole response down.

  * `sanitize_non_finite(obj)` returns a copy in which every NaN / +-Infinity float is replaced by `None`
    (JSON `null`), recursing through dicts, lists, tuples and sets. Everything else is returned unchanged.
  * `SafeJSONResponse` is the application's default response class: it sanitises before rendering, so every
    endpoint is covered without each route having to remember to.

`null` is the honest value: "no valid number" is not the same as 0 or 100, and the scorer separately treats a
non-finite dimension like a failed one (see `EnhancedScorer`), so a core score is never silently invented.
"""
import logging
import math
from typing import Any

from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


def _clean(value: Any, counter: list) -> Any:
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        counter[0] += 1
        return None
    if isinstance(value, dict):
        return {k: _clean(v, counter) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_clean(v, counter) for v in value]
    return value


def sanitize_non_finite(value: Any) -> Any:
    """Copy of `value` with every NaN / +-Infinity float replaced by None."""
    return _clean(value, [0])


class SafeJSONResponse(JSONResponse):
    """JSONResponse that turns NaN / Infinity into null instead of failing the request."""

    def render(self, content: Any) -> bytes:
        counter = [0]
        cleaned = _clean(content, counter)
        if counter[0]:
            # Never log the values or the payload (it can contain personal data); the count is enough to notice.
            logger.warning("Replaced %d non-finite number(s) with null in a JSON response.", counter[0])
        return super().render(cleaned)
