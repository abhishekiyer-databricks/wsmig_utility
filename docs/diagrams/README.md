# Diagrams

The docs embed **pre-rendered PNGs** from this folder so they display in **any** Markdown viewer —
PyCharm, VS Code, Cursor, Azure DevOps Wiki, GitHub — with **no plugin or setup**.

The editable source for every diagram is [Mermaid](https://mermaid.js.org/), kept in
[`src/`](src/). PNGs are the generated artifact; **edit the `.mmd`, not the `.png`.**

## Regenerate

Requires Node (for `npx`). No global install needed.

```bash
# from docs/diagrams/
for f in src/*.mmd; do
  name=$(basename "$f" .mmd)
  npx -y @mermaid-js/mermaid-cli \
    -i "$f" -o "$name.png" \
    -c src/mermaid-config.json \
    -p src/puppeteer-config.json \
    -b white -s 3
done
```

- `-b white` bakes a white background so text is readable in light **and** dark editor themes.
- `-s 3` renders at 3× for crisp text.
- `src/mermaid-config.json` sets the shared font/theme; `src/puppeteer-config.json` passes
  `--no-sandbox` for headless environments.

## Diagram index

| PNG | Used in | Shows |
|-----|---------|-------|
| `overview.png` | root README | Source assets → bundle → target |
| `pick-your-path.png` | docs/README | Which guide to read |
| `happy-path.png` | docs/README | Direct-mode end-to-end flow |
| `big-picture.png` | ARCHITECTURE | End-to-end working model |
| `airgap.png` | ARCHITECTURE | Airgap two-sided model + handoff |
| `direct.png` | ARCHITECTURE | Direct one-sided model |
| `pipeline-stages.png` | ARCHITECTURE | Inventory / export / import |
| `dependency-order.png` | ARCHITECTURE | Asset creation order (ACLs last) |
| `incremental.png` | ARCHITECTURE | Upsert create/update/skip decision |
| `preflight.png` | ARCHITECTURE | Preflight go/no-go checks |
| `auth-model.png` | ARCHITECTURE | Token vs OAuth M2M |
| `config-flow.png` | CONFIGURATION_GUIDE | Widgets → validated Config |
| `identities.png` | PERMISSIONS_GUIDE | Identities per mode |
| `account-admin-flow.png` | PERMISSIONS_GUIDE | Account-admin optional path |
| `which-mode.png` | RUNBOOK | Choose airgap vs direct |
| `direct-sequence.png` | RUNBOOK | Direct-mode step sequence |
| `airgap-sequence.png` | RUNBOOK | Airgap-mode step sequence + handoff |
| `retry-loop.png` | RUNBOOK | Fix → retry_mode=failed_only |
