"""The application layer: what the CLI *does*, independent of how it is typed.

A command module answers "which flags did the user pass"; a use case here answers
"what does an upgrade / a task pause / a log export mean". The split is what lets
the same behaviour be driven by `atf rules pause web`, by the wizard, or by a
test with an in-memory gateway — the three callers cannot drift, because there is
only one implementation to drift from.

Modules
-------
``results``       the outcome of a mutation: JSON payload + the sentence to print
``use_cases``     one module per bounded context, each a small application service
"""

from __future__ import annotations

from .results import Outcome, Warning_

__all__ = ["Outcome", "Warning_"]
