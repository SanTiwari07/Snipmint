"""Optional YouTube channel integration: connect a channel, publish clips as Shorts.

Everything here is off unless GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI
are set. Talks to Google with plain httpx (no Google SDK needed). OAuth tokens are encrypted at
rest, kept apart from projects and exports, never returned by the API and never logged.
"""
