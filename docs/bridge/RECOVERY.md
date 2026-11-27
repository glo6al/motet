# Clearing an uncertain Bridge attempt

An unknown delivery is not a failed send or a successful reply. It may already have run. Motet never resends it automatically.

When Motet has already identified the exact native Codex turn, a readable end event for that same turn releases the reservation even if a transcript gap prevents a trustworthy reply. The result stays unknown and the reply is withheld. A missing binding, unreadable end, or unrelated turn ending does not release it. Being idle alone is insufficient.

For a stuck attempt that cannot settle automatically, an owner can retire its reservation through the running Motet server. Retirement does **not** cancel the native message, prove it finished, or send it again. Its outcome remains unknown and any late reply is withheld. Other outstanding attempts can still block that recipient.

Run a Motet server version that includes the recovery routes before using this command. If Motet runs on a custom port, supply it in place of the default port shown below.

## Inspect first

Use the full transport UUID shown in the blocked-recipient error, not the requesting client's logical request ID or a shortened prefix. Supply the Motet port explicitly:

```sh
python3 scripts/watch_bridge_clear.py --port 8793 \
  --transport-id 11111111-1111-4111-8111-111111111111
```

The fictional ID above must be replaced with the exact blocked attempt. Inspection returns its exact recipient, execution/capture state, payload-retention flag and retirement eligibility. It changes nothing and does not include the message, answer or local paths. Eligibility is checked again atomically when applying.

## Explicitly retire

After verifying the exact attempt and recipient, use the returned recipient ID:

```sh
python3 scripts/watch_bridge_clear.py --port 8793 \
  --transport-id 11111111-1111-4111-8111-111111111111 \
  --expected-agent x:22222222-2222-4222-8222-222222222222 --apply
```

This command only retires an uncertain launched attempt, or confirms an identical previous retirement. It refuses known-active/unlaunched attempts, completed/dropped results and mismatched recipients. Native work may still continue after retirement; this is an explicit owner decision to release an uncertain reservation, not evidence of cancellation.

If the response is lost, the retirement outcome is unknown. Inspect the same transport ID. Repeating the same exact retirement is idempotent; creating a new peer request is a different action and could duplicate work.

The CLI reads only the existing private owner key from the default Motet state directory (or `--state-dir`). It does not print the key or place it in arguments. It connects to loopback only, with no proxies, redirects, retries, provider calls, or offline database fallback. The owner key is required; the shared MCP credential cannot clear requests. The command currently requires a POSIX host with a private key file.

Retirement preserves immutable admission, launch and deduplication records. Pruned payloads remain absent. Stale tracker writes cannot restore withheld replies. The existing offline recovery utility is separate and remains available for reviewed maintenance; this command never invokes it.

See `docs/BRIDGE_CHANGES.md` for the history of bridge changes and known open causes.
