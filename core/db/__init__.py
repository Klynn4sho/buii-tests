"""Database infrastructure split from the domain/query facade.

`core.database` remains the compatibility API used by cogs and views, while
connection-pool lifecycle lives here so storage infrastructure can evolve
independently from feature-specific SQL.
"""
