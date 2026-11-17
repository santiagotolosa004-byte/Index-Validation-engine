"""Index calculation & data-validation engine.

A small, end-to-end replica of an index-production workflow:
raw prices + corporate actions -> data validation -> remediation ->
corporate-action adjustment -> index calculation -> independent
(shadow) recalculation -> benchmark comparison -> report.
"""

__version__ = "0.1.0"
