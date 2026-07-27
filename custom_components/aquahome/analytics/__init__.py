"""AquaHome analytics tier: baselines and detectors over imported statistics.

The package splits along the executor boundary: :mod:`.series`, :mod:`.baseline`
and :mod:`.detectors` are pure computation (numpy + stdlib, no Home Assistant
imports) executed off the event loop, :mod:`.model` holds the frozen types they
exchange, and :mod:`.engine` is the Home Assistant-facing coordinator that
gathers inputs, dispatches the computation and publishes the result.
"""

from .model import AnalyticsInputs, AnalyticsResult, NightVerdict

__all__ = [
    "AnalyticsInputs",
    "AnalyticsResult",
    "NightVerdict",
]
