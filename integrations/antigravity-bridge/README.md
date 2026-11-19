# Motet bridge for Antigravity

Antigravity chats can use this plugin to send owner-authorized requests to Codex or Claude and retrieve replies by request ID. An optional shared inbox stores incoming requests at any time. Nothing makes an Antigravity chat check it; an active chat checks only when explicitly asked or at an authorized work checkpoint. It belongs to the Antigravity connection, rather than a verified native chat, and never wakes an idle chat.

On macOS or Linux, use a stable Motet checkout with the MCP adapter dependencies installed, a Node.js executable, and an existing shared Motet bridge credential. From that checkout, run the installer with the credential **file path** and Node.js executable path.

```sh
python3 scripts/install_antigravity_bridge.py install \
  --credential "/path/to/shared-credential.json" \
  --node "$(command -v node)"
```

The installer writes Antigravity's `motet-bridge` plugin under `~/.gemini/config/plugins/`. It uses the credential in place. Its JSON stores the executable, adapter, credential path, and adapter working directory. For a stable checkout separate from the installer, pass `--repo "/path/to/motet"`.

At registration, the installer checks the Unix owner and permissions of Node and its symlink targets, the checkout paths to `peer.mjs` and `inbox.mjs` when present, and the receiver manifest when requested. Under the adapter's ancestor directories, it checks any existing `node_modules`, `@modelcontextprotocol` scope, SDK directory, and SDK `package.json`. The checked paths and their components must be controlled by you or root and protected from group and other writes. A root-owned sticky ancestor can be traversed to a separately checked entry. A trusted Node symlink stays in the configuration so upgrades can update its target.

Use a protected Motet checkout and Node and dependencies from trusted installations. Registration checks are limited to the paths above and Unix owner and mode bits. SDK `dist/` contents, sibling dependencies, ACL-based write access, and changes after registration fall outside those checks. Your home, `~/.gemini`, `~/.gemini/config`, and any existing `plugins` directory must be real directories owned by you without group or other write permissions. The installer reports the refused launch or plugin directory path and preserves its permissions.

In Antigravity, open **Customizations → Installed** and find `motet-bridge`. It is enabled by default unless Antigravity has a saved preference for it. Check **MCP Servers** in a chat for the plugin's `list_agents`, `send_message`, `get_reply`, and `list_requests` tools. Running `list_agents` confirms Motet connectivity without sending a peer message.

`python3 scripts/install_antigravity_bridge.py status` reports registration on disk. Disable the plugin in Antigravity before running `python3 scripts/install_antigravity_bridge.py remove`. Removal checks the exact installed file hashes and preserves modified or foreign files. After removal, rerun `install` to change the credential path, Node executable, or checkout. The shared credential and Antigravity's other settings are managed separately.

Installation and removal change the registered plugin directory atomically. After a forced interruption, retry the same command. An interruption can leave an inert private `.motet-bridge-*` staging directory that Antigravity does not load.

To generate the JSON configuration for a manual plugin setup, run `python3 scripts/watch_peer.py config "/path/to/shared-credential.json" --client antigravity`. See [Motet bridge setup](../../docs/PEER_BRIDGE.md) for shared service configuration.

## Enable the shared inbox

Create its separate private receiver capability, then install the plugin with that file:

```sh
python3 scripts/setup_antigravity_inbox.py init
python3 scripts/install_antigravity_bridge.py install \
  --credential "/path/to/shared-credential.json" \
  --node "/path/to/trusted/node" \
  --inbox-credential "$HOME/.live-room/antigravity-inbox.json"
```

If an outbound-only plugin is already registered, disable and remove that registration first. The installer preserves both credentials. Restart Motet to load the inbox configuration, then refresh the clients' MCP connections. Initialization preserves an existing inbox and key; it does not rotate them.

At each MCP connection, the adapter checks whether Motet has a configured inbox. Without one, or if that check cannot be confirmed, it offers only the four native tools and native instructions. With one, it also offers the four inbox sender tools. Refresh the MCP connection after enabling or disabling an inbox. Receiver tools additionally require a matching private receiver capability.

Codex and Claude use `list_inboxes`, pin its ID and revision, leave a request with `send_inbox_message`, and retrieve its answer with `get_inbox_reply`. `list_inbox_requests` helps recover a lost request ID. A queued request means only that it was stored.

Antigravity checks previews with `check_inbox` at a meaningful work checkpoint. Before claiming work it calls `prepare_inbox_claim` and retains the private token, then confirms `claim_inbox_message`. Previews expose up to 160 characters to every connected client and can contain an entire short message. Reading a preview is not a claim or authorization to act. A confirmed claim records responsibility and returns the full message. It records its answer with `answer_inbox_message`; a native final `WATCH_REPLY` marker does not answer an inbox. Never put the claim token in peer traffic or summaries. The receiver tools appear only when its separate capability is configured.

A claimed request means someone explicitly picked it up, not that Motet verified a native turn. A replied request is the receiver's recorded answer. If an acknowledgment is lost, retain the original request ID and token for explicit recovery; recovering a claim does not establish whether work already ran. No claim is automatically transferred or repeated. A lost token cannot be recovered from a listing.

Inbox message bodies, previews and answers expire 24 hours after creation, including when a reply arrives near the cutoff. Admission and answer recording also require the exact encoded result to fit 60,000 UTF-8 bytes, including metadata and JSON escaping; fewer than 16,000 characters can reach that byte cap. Cleanup occurs while Motet runs and at startup after downtime; expired bodies cannot be read meanwhile. Future cleanup overwrites deleted payload records in the main SQLite database. Earlier deletions and copies in write-ahead logs, rollback journals or backups are outside that cleanup. Small deduplication records remain, preventing the same request ID from being sent again. Capacity is bounded separately from native execution slots, with shared send-rate limits.

To disable inbox receiving, stop Motet, move the private inbox configuration out of the state folder, and restart. Historical sender reads remain available until expiry. Replacing the key changes the revision and cannot transfer old queued or claimed work to the new key. Restoring the original configuration restores that revision's access within retention. Disabling this plugin alone stops its checks but leaves the configured mailbox able to queue requests.
