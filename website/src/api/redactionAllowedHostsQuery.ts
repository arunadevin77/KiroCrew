/**
 * React Query key for the redaction allowed-hosts list (Settings → Security →
 * Redaction). The Settings card reads it and a blocked-link card refreshes it
 * after an allow or undo, so both spell the key from here: a rename in one
 * place would otherwise leave the other refreshing a key nothing reads.
 */
export const REDACTION_ALLOWED_HOSTS_QUERY_KEY = ['redaction-allowed-hosts'] as const
