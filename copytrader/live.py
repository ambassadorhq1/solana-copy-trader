"""LIVE mode: deliberately a stub. Disabled in v1.

It does not read, load, or accept private keys, and it never signs or sends transactions.
When live trading is built (after 1-2 weeks of paper results), the plan in docs/architecture.md applies:
dedicated hot wallet only, key from server env or the Hark vault (never chat), Jupiter swap build ->
sign -> Helius Sender, capped priority fee, the same caps and exits as paper mode.
"""


class LiveExecutorDisabled(RuntimeError):
    pass


class LiveExecutor:
    def __init__(self, *_, **__):
        raise LiveExecutorDisabled("LIVE mode is disabled in v1. Set mode: paper in config.yaml.")
