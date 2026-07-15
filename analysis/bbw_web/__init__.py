"""bbw_web — multi-user BFF + UI, isolated from bbw_protocol core.

Import protocol only at the edges (store / bff). Never put multi-tenant
logic into bbw_protocol.
"""

__version__ = "0.2.0"
