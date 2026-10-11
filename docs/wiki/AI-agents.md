# AI agents

Homestead can be driven by an AI agent: Claude Code, Claude Desktop or any
other client that speaks the [Model Context Protocol](https://modelcontextprotocol.io)
(MCP). The agent can look at your cluster, start and stop things, deploy and
edit containers and VMs, and work with volumes and backups, within what you let
its key do.

It is **off by default**, and careful by design: every change is checked first,
anything that destroys data waits for a person to approve it, and every call is
written to an audit.

For scripts, Home Assistant, or agent frameworks that do not speak MCP, use the
[API](API). MCP is the easier way for clients that support it; the API, with its
OpenAPI description, remains for the rest.

## Turning it on

An administrator opens **Settings › Access** and turns on the **AI agents (MCP)**
card. While it is off, `POST /mcp` answers `404`, as if it did not exist.

## Making a key

An agent signs in with a [Homestead API key](API#making-a-key), made in
**Settings › Users and access › API keys › ＋ New key** with **Allow AI agents (MCP)**
ticked. A key without that tick does not work on `/mcp`, and a browser session
never does.

A key acts as an **operator at most**, even when an administrator made it.
Choose scopes for what the agent should do:

| Scope | Lets the agent |
|---|---|
| `read` | See the cluster: nodes, containers, VMs, volumes, backups, alerts and jobs |
| `logs:read` | Read containers' logs (they can hold passwords apps print, so it is separate) |
| `containers:control` | Start, stop and restart containers |
| `containers:manage` | Deploy, install, edit, scale and move containers; ask to delete one |
| `vms:control` | Start, stop and restart virtual machines |
| `vms:manage` | Create, edit and migrate VMs; ask to delete one |
| `storage:manage` | Create and grow volumes, snapshot and back up; ask to delete, revert or restore |

Give the agent `read` alone first and see what it makes of your cluster. Add
scopes when you want it to act. Use HTTPS: the key travels in a header. Set
**Only from** to the client's address only where Homestead can see it (see
[API](API) - on many clusters it sees a node's address instead).

## Connecting a client

The address is `/mcp` on your Homestead, and the key goes in the
`Authorization: Bearer` header.

**Claude Code:**

```bash
claude mcp add --transport http --scope user homestead http://homestead.example.com/mcp \
  --header "Authorization: Bearer hsk_..."
```

`--scope user` makes Homestead available in every project. Without it, Claude
Code adds it only to the folder the command ran in. The desktop app's Code tab
reads the same configuration, but does not put the `claude` command on the
path: install it with `npm install -g @anthropic-ai/claude-code` to run the
line above. A session loads its servers when it starts, so start a new one and
run `/mcp` to see Homestead connected.

**Claude Desktop** reaches it through `mcp-remote`. Add this to its
configuration:

```json
{
  "mcpServers": {
    "homestead": {
      "command": "npx",
      "args": ["mcp-remote", "http://homestead.example.com/mcp",
               "--header", "Authorization: Bearer ${HOMESTEAD_KEY}"],
      "env": { "HOMESTEAD_KEY": "hsk_..." }
    }
  }
}
```

The server speaks MCP Streamable HTTP, statelessly: `initialize`, `tools/list`,
`tools/call` and `ping`, each as one JSON request and answer. `GET` is refused
with `405`. A request may be at most 256 KiB, and batches are not accepted.

The agent sees only the tools its key's scopes allow.

## The tools

Tools come in three classes:

- **read** - looks, changes nothing.
- **change** - changes something, and is checked first (see below).
- **destructive** - cannot be undone, so it only *asks*: a person approves it in Homestead.

### Cluster

| Tool | What it does | Scope | Class |
|---|---|---|---|
| `get_agent_request` | Follows a request a destructive tool filed: pending, then done, denied, expired or failed | any key | read |
| `whoami` | This key: its name, its scopes and when it expires | any key | read |
| `get_status` | The cluster at a glance: nodes, containers, VMs and alerts, counted | `read` | read |
| `list_nodes` | Every host with its health and use | `read` | read |
| `list_alerts` | What is wrong right now, as the app's bell shows it | `read` | read |
| `get_job` | A background job by the id an action answered with | `read` | read |
| `storage_forecast` | When each volume, disk and the pool is expected to fill | `read` | read |
| `host_failure_forecast` | For every host: if it failed, whether the others would have room for what it runs, tier by tier | `read` | read |

### Containers

| Tool | What it does | Scope | Class |
|---|---|---|---|
| `list_containers` | Every container (app) with its state | `read` | read |
| `get_container` | One container's state | `read` | read |
| `get_container_settings` | A container's settings as Edit shows them: image, ports, variables, volumes, resources, placement | `read` | read |
| `list_events` | Kubernetes' recent events for a container or VM, newest first | `read` | read |
| `get_container_logs` | A container's last lines of output (at most 500) | `logs:read` | read |
| `deploy_options` | What a new container can use: storage classes, networks, hosts, hardware, addresses | `read` | read |
| `search_app_store` | Apps the App Store offers, by name or category | `read` | read |
| `container_start` | Starts a stopped container, once Homestead has checked it fits | `containers:control` | change |
| `container_stop` | Stops a container (scales it to zero) | `containers:control` | change |
| `container_restart` | Restarts a container's pods | `containers:control` | change |
| `preview_deploy` | What deploying these settings would create, and its capacity review, without creating anything | `containers:manage` | change |
| `deploy_container` | Deploys a container with the settings preview_deploy reviewed, its capacity_token and, when the review asked for it, confirm_capacity: true | `containers:manage` | change |
| `install_app` | Installs an App Store app, with overrides to its defaults | `containers:manage` | change |
| `preview_compose` | What a Docker Compose file would deploy, and its review, without deploying it | `containers:manage` | change |
| `apply_compose` | Deploys a Compose file as preview_compose reviewed it | `containers:manage` | change |
| `preview_edit_container` | What saving these settings (from get_container_settings, changed) would do: what restarts, and the capacity review | `containers:manage` | change |
| `edit_container` | Saves a container's settings as preview_edit_container reviewed them | `containers:manage` | change |
| `scale_container` | Sets how many copies of a container run | `containers:manage` | change |
| `preview_move_container` | Whether a container fits on another host, and its capacity_token for move_container | `containers:manage` | change |
| `move_container` | Moves a container to another host as preview_move_container reviewed it | `containers:manage` | change |
| `delete_container` | Asks to delete a container and its Services; its volumes are kept | `containers:manage` | destructive |

### Virtual machines

| Tool | What it does | Scope | Class |
|---|---|---|---|
| `list_vms` | Every virtual machine with its state | `read` | read |
| `get_vm` | One virtual machine's state | `read` | read |
| `vm_start` | Starts a VM, once Homestead has checked it fits | `vms:control` | change |
| `vm_stop` | Asks the guest to shut down | `vms:control` | change |
| `vm_restart` | Restarts the guest | `vms:control` | change |
| `preview_create_vm` | What creating a VM with these settings would make, and its review; pass its capacity_token to create_vm | `vms:manage` | change |
| `create_vm` | Creates a VM as preview_create_vm reviewed it | `vms:manage` | change |
| `preview_edit_vm` | What saving these VM settings would change, and its review | `vms:manage` | change |
| `edit_vm` | Saves a VM's settings as preview_edit_vm reviewed them | `vms:manage` | change |
| `migrate_vm` | Live-migrates a running VM to another host | `vms:manage` | change |
| `delete_vm` | Asks to delete a VM, and its disks when delete_disks is true | `vms:manage` | destructive |

### Storage

| Tool | What it does | Scope | Class |
|---|---|---|---|
| `list_volumes` | Every volume: size, use, health, copies and what uses it | `read` | read |
| `list_snapshots` | A volume's snapshots | `read` | read |
| `list_backups` | Backups, of one volume or all | `read` | read |
| `create_volume` | Creates an empty volume | `storage:manage` | change |
| `edit_volume` | Grows a volume, or changes how many copies Longhorn keeps | `storage:manage` | change |
| `snapshot_volume` | Takes a snapshot of a volume now | `storage:manage` | change |
| `backup_volume` | Backs a volume up to the backup target now | `storage:manage` | change |
| `run_backup_job` | Runs a recurring backup or snapshot job now | `storage:manage` | change |
| `delete_volume` | Asks to delete a volume; with delete_data its data is deleted too, not only the claim | `storage:manage` | destructive |
| `delete_snapshot` | Asks to delete a snapshot | `storage:manage` | destructive |
| `revert_to_snapshot` | Asks to roll a volume back to a snapshot: what was written since is lost, and what uses it stops meanwhile | `storage:manage` | destructive |
| `restore_backup` | Asks to restore a backup as a new volume | `storage:manage` | destructive |
| `delete_backup` | Asks to delete a backup from the backup target | `storage:manage` | destructive |
| `change_storage_class` | Asks to move a volume's data to another storage class; what uses it stops while it copies | `storage:manage` | destructive |

Each tool uses the same routes and checks as the web app, so what Homestead
would refuse a person it refuses the agent.

## How changes work

A change is made in steps, as in the web app:

1. **Read the settings.** For an edit, `get_container_settings` gives the
   settings as the Edit dialog shows them. `deploy_options` lists what a new
   container can use.
2. **Preview.** `preview_deploy`, `preview_compose`, `preview_edit_container`,
   `preview_move_container`, `preview_create_vm` and `preview_edit_vm` say what
   would happen - what restarts, whether it fits - and change nothing. A preview
   that checks capacity answers with a `capacity_token`.
3. **Apply.** The matching tool takes the preview's `capacity_token`, and
   `confirm_capacity: true` when the review asked for it. If the cluster has
   changed since the preview, Homestead refuses and the agent previews again.

Edits are recorded in the change history and can be undone there.

## Approvals

A destructive tool does not run. It files a **request** that names the action in
a sentence, with the reason the agent gave, and answers with a request id. The
agent follows it with `get_agent_request`.

- The request appears in the **bell** and is **pushed to devices** in the
  **AI agents** notification category.
- It is approved or denied on the **/agents** page, by anyone whose role allows
  the action. Deleting a VM or a volume, and anything else the web app keeps for
  administrators, needs an administrator.
- Changes that need an administrator (host access, privileged mode) are
  requests too.
- On approval Homestead **plans again as the approver** and runs only if the
  plan is unchanged. If the cluster moved on, the request ends as failed and the
  agent can ask again.
- A request expires after **30 minutes**, and a key may have at most **10**
  waiting.

## Security

- **Off until you turn it on**, and `404` while it is off.
- **Keys only.** A browser session never works on `/mcp`, and a key needs the
  **Allow AI agents (MCP)** tick. Like every key it expires, and can be limited
  to addresses.
- **Origin.** A request that carries an Origin must come from Homestead's own.
- **Operator at most.** Whoever made the key, it never acts as an administrator.
  Admin-only actions go through an approval.
- **Scopes decide the tool list.** An agent cannot call what it cannot see.
- **Rate limits**, per key per minute: 120 reads, 20 changes, 5 destructive.
- **Size.** Requests are at most 256 KiB, and there are no batches.
- **App text is data.** Names, events, logs and alerts are written by your apps
  and users, so the agent is told to treat them as data, not instructions. Logs
  come back marked untrusted, with control codes stripped, at most 500 lines.
- **Never available to agents:** users, keys and settings; shells and exec;
  Secrets; the file manager; host power and updates; Homestead itself; and linked
  clusters. Hosts are read-only.
- **Audited.** Every call is recorded (below).

Use HTTPS, or limit the key to the client's address: a key sent over plain HTTP
can be read by anyone on the path. Limiting by address works only where
Homestead sees clients' own addresses; the API keys card says when it does not.

## The audit

Every call a key makes is kept for **30 days**: MCP calls, and the changes and
refusals on `/api/v1`. Secrets and environment values are hidden. It is shown in
Settings.

## Troubleshooting

| You see | It means |
|---|---|
| `404` | AI agents (MCP) is switched off in Settings › Access |
| `401` | No key, or an invalid, revoked or expired one |
| `403` | The key was not made with **Allow AI agents (MCP)**, or the request's Origin is not Homestead's own |
| `405` | `GET` was used; MCP clients use `POST` |
| `429` | A rate limit, or too many wrong keys from one address |
| A tool is missing | The key's scopes do not include it; make a key with the scope |
| A request ended as failed | The cluster changed between the ask and the approval, so the plan no longer matched. Ask again |
| A request expired | Nobody answered within 30 minutes |
| `401 … cannot be used from 10.42.0.0` | Homestead sees a node's address, not the client's, so **Only from** cannot match. Make a key without it (see [API](API)) |
