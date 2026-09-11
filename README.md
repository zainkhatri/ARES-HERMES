# PROMETHEON

Self-hosted **mission-control dashboards** for a three-machine homelab — **ARES**, **EROS**, and **ZEUS**.

One Flask app, three full-screen themed views (red / amber / blue). Each renders its own box: live
telemetry, GPU, storage, running services — and a traversable **3D knowledge graph** of what actually
lives on that machine, each node annotated with an AI-written description of the folder it represents.

---

## The dashboards

### ARES — the host (red)
Proxmox host: runs the dashboard, the photo library, and the shared RTX 3080.

![ARES dashboard](docs/screenshots/ares.png)

### EROS — the worker (amber)
Ryzen + GTX 1070 box that runs the business container stack. Its "operation modules" are the live
containers, not apps.

![EROS dashboard](docs/screenshots/eros.png)

### ZEUS — the vault (blue)
Backup mule that wakes on a timer, pulls nightly copies of ARES + EROS, and sleeps. Its view centers on
the **combined** knowledge graph — the union of all three machines.

![ZEUS dashboard](docs/screenshots/zeus.png)

---

## Lineage

It started as **prometheon**. It became **nexus**. Then I split it into **ARES** + nexus, built **EROS**,
and it settled into three machines: **ARES · EROS · ZEUS**.

> "nexus" was the earlier name for the third box — it's **ZEUS** now. The name survives here as history.

The repo name (`ARES-HERMES`) and the pool path (`PROMETHEUS`) are fossils from earlier stops on that
road. Same project, more machines.

---

## What's inside

- **One template, three brands.** `templates/home.html` renders per-box via `data-brand`; a switcher
  (top-left) and a live clock (top-right) tie the three views together. Each box pulls its own data —
  ARES from its host APIs, EROS/ZEUS from a shared fleet snapshot — so no view ever shows another box's
  numbers.
- **Knowledge graph (MNEMOSYNE).** A stdlib-only Python indexer walks each box's filesystem to a bounded
  depth, fingerprints folders, and asks a local LLM (Ollama) to write a one-line "understanding" of each
  one — vault directories are name-only, never scanned. The result is a force-directed 3D graph you can
  spin, zoom, and click: pick a node to read its write-up, or open fullscreen to focus-and-spotlight a
  subtree. A nightly job keeps it current, and an always-on MCP server exposes it to any agent session
  (`kg_search`, `kg_get`, `kg_neighbors`, `kg_tree`, `kg_stat`).
- **Fleet telemetry.** CPU / memory / load / thermals / GPU relay per box, plus a storage "where it
  lives" breakdown.
- **The apps behind the tiles.** A CLIP-searchable photo gallery with face clustering, a GoodNotes
  journal browser, a file explorer, and a web terminal — all behind session / bearer-token / WebAuthn
  auth, with an encrypted vault gate for private items.

## Stack

Flask (single-process, gunicorn) · SQLite (WAL) · Caddy (TLS + static serving) · open-clip / face
recognition for the gallery · Ollama for the graph's descriptions · Proxmox underneath it all. No
frontend framework — the graph and radars are hand-rolled `<canvas>`.

---

*A personal homelab project. Open-sourced as a reference for anyone building their own mission-control
layer over a home server.*
