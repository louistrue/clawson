"""Direct API clients (Path B): no OpenClaw MCP indirection.

Each client is a thin async wrapper around its provider's REST API,
exposing only the endpoints Clawson actually consumes. Pollers in
`briefing/` translate raw responses into normalised Events.
"""
