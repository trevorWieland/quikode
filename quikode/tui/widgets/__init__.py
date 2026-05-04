"""TUI widgets. Each widget is responsible for *rendering* one panel.

Widgets do NOT reach into SQLite directly — they consume snapshots fed by
`controllers.store_polls` so the polling cadence stays centralized.
"""
