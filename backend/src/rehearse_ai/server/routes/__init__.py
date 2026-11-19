"""HTTP routes, one module per capability. Routes map requests to the capability packages.

Business rules live in those packages (``scheduling``, ``invitations``, ``records``, ...); a
handler here authenticates, validates the request shape, calls one of them and shapes the response.
"""
