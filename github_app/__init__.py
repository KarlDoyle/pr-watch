"""PR Watch — GitHub App mode.

Additive to the single-team CLI/polling mode in ``pr_watch.py`` at the repo
root. This package turns PR Watch into a real GitHub App: a webhook
receiver for ``pull_request`` events (``review_requested`` specifically —
this is what replaces Search-API polling, per the earlier technical audit)
plus App JWT / installation-token handling.

Nothing here is registered with GitHub yet — see ``manifest.json`` and the
"Manual setup" section of the repo README for the exact steps to finish
registration from a GitHub account/org.
"""
