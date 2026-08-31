"""prizmanyx-foundry: a standalone estate-manufacturing and pipeline-loop tool.

See README.md for the three boundaries this package is built inside of. In
short: this is not part of the Prizmanyx application, it drives Prizmanyx only
through its public API, and it stands up Floci containers that get a
docker-socket mount local dev machines should treat as host-root-equivalent
access.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
