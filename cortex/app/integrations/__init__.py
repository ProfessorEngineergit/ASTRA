"""External capability clients used by ASTRA's tools.

Every integration follows the same contract as the rest of cortex: it degrades to
a graceful no-op / clear message when its credentials are not configured, so the
app always boots and unconfigured features simply announce themselves as off.
"""
