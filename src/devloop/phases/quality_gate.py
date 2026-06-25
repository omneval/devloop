"""QualityGate — pure-function decision module for code quality threshold checks.

This module contains **no I/O** and **no Temporal dependencies**.
It simply evaluates whether a sentrux score meets a threshold.
"""

from __future__ import annotations

from typing import Literal

__all__ = ["QualityGate"]


class QualityGate:
    """Pure-function decision module: ``check(score, threshold) -> 'pass' | 'fail'``.

    Stateless — every call is a pure function of its arguments.
    """

    @staticmethod
    def check(score: int, threshold: int) -> Literal["pass", "fail"]:
        """Return ``'pass'`` when *score* meets or exceeds *threshold*.

        Parameters
        ----------
        score: int
            The sentrux score (0–10000 native scale).
        threshold: int
            The quality threshold (0–10000 native scale).

        Returns
        -------
        Literal["pass", "fail"]
            ``'pass'`` if ``score >= threshold``, else ``'fail'``.
        """
        return "pass" if score >= threshold else "fail"
