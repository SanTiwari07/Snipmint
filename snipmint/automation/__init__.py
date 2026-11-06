"""Optional workflow automation with n8n.

Snipmint stays the source of truth: it owns metadata, approval, OAuth, uploads and every status.
n8n only orchestrates (when to publish, batches, retries, notifications) by calling the
``/api/integrations/n8n/*`` endpoints with a shared secret, and receives signed events from
Snipmint. n8n never sees Google credentials, tokens or the Snipmint access token.
Off unless SNIPMINT_N8N_WEBHOOK_SECRET is set.
"""
