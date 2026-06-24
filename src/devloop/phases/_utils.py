"""Internal utilities for phase modules.

This module is private to the ``phases`` package (``_utils`` prefix).  It
provides helper functions that keep phase implementations DRY.
"""

from __future__ import annotations


def callback_or_ops(
    ops_cb,
    cb_cb,
    default_cb=None,
):
    """Select a callback from ops → cb → default priority.

    Phase implementations frequently need to pick between an ``ops`` override
    (set by the caller for that specific invocation) and a ``cb`` field on
    the ``PhaseOps`` protocol that the workflow wired at startup.  The rule
    is always the same: *ops wins if set, otherwise cb, otherwise default*.

    Examples
    --------
    >>> def noop(): ...
    >>> callback_or_ops(None, noop, noop) is noop  # cb
    True
    >>> callback_or_ops(noop, lambda: None, noop) is noop  # ops
    True
    """
    if ops_cb is not None:
        return ops_cb
    if cb_cb is not None:
        return cb_cb
    return default_cb
