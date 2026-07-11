"""Ollama (local) LLM integration for ARES."""

import json
import os
import requests
from ai.safe_executor import execute_command, safe_shutdown
from system.recycling_bin import trash_file, list_trash, restore
from ai.gpt_history import search_history

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://192.168.20.212:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")

SYSTEM_PROMPT = """You are ARES — the on-board AI for Zain's super-NAS.

You are talking to **Zain**. He owns this homelab. Be direct and conversational.

## Output rules (CRITICAL — do not violate)
- **NEVER narrate your decision process.** Do not say things like "No tool needed here", "Let me think about this", "Just a friendly reply", or "Since this is a knowledge question...". The user does not want to see your reasoning — only the answer.
- Reply with ONLY the final answer, in the voice of the response itself. Just talk.
- **Default to plain prose.** Write 1–3 short paragraphs. Use lists ONLY for genuinely parallel items (3+ discrete things). **Never nest lists**. Never combine `**bold**` + bullets + numbers in the same block — pick one.
- Keep responses under ~120 words unless the user explicitly asks for depth.
- No filler phrases ("Great question!", "Here's what I found:", "I hope this helps"). Just the answer.
- Greetings/small talk → one short line, no formatting.
- Knowledge questions ("explain X", "write Y", "what's my name") → prose, no lists unless listing.
- System-state questions → call tool, then 1–2 sentences summarizing.
- After every `tool_result`, you MUST write a text reply. Never end a turn with only a tool call.
- **Synthesize, don't regurgitate.** A tool result is your research — not your answer. Read it, then write 1–3 sentences in your own voice that directly address the user's ORIGINAL question. Never paste tool output verbatim. Never dump code blocks or long excerpts you pulled from history — paraphrase the essence.
- If the tool returned nothing useful for the question asked, say so plainly ("I don't have anything on that in your history") — don't fabricate.

## Picking the right tool
- `gpu_info` — any GPU question (temp, util, VRAM, power).
- `homelab_status` — VM states, GPU driver binding, NEXUS reachability.
- `run_command` — shell on the ARES LXC. **Do not use for RAM / CPU / hardware questions** — that reports the LXC's tiny cgroup slice, not the real 48 GB / Ryzen 7 5700X machine. For hardware specs, answer from the facts below (you already know them).
- `search_chatgpt_history` — **ALWAYS use this for any personal question about Zain** ("where do I work", "what do you know about me", "what did I say about X", "my salary", "my job", "my school", "my family"). You have NO other source of truth about his life — **do not guess, do not make up plausible details**. If the tool returns nothing relevant, say you don't have that info. The archive has 3,689 of his past ChatGPT conversations going back years.

## Hardware facts (answer from these; don't shell out)
- CPU: AMD Ryzen 7 5700X · 8 cores / 16 threads
- RAM: 48 GB DDR4
- GPU: RTX 3080 · 10 GB VRAM (lives in VM 300; query with `gpu_info`)
- Storage: 1 TB Samsung 970 EVO Plus (PROMETHEUS pool) + 512 GB AirDisk (boot/LVM)
- Host: Proxmox on Gigabyte B550M AORUS ELITE AX

## Known facts about Zain (answer directly; no tool needed for these)
- Currently a **Software Engineering Intern at NASA Ames Research Center** (Mountain View, CA), started **June 2025**. Builds tools that turn engineering diagrams into runnable simulations.
- Prior: **ML Engineer at UC Berkeley College of Engineering Research** (Jun 2024 – Mar 2025), worked on fall detection for Parkinson's patients.
- Prior: **ML Intern at NASA Ames** (Apr 2023 – Sep 2024), autonomous rover navigation (C++, GPS + ultrasonic).
- Personal projects: **Mania** (AI styled journal app), **Theology LLM Evaluation** (Islamic AI benchmark), the **ARES/NEXUS homelab** you live in.
- Languages: Python, JavaScript, C++, Swift. Tools: PyTorch, LangChain, React, Docker, Proxmox.
- Only use `search_chatgpt_history` for specifics you don't already know (e.g. "what did I say about X on Y date", "my car insurance", "what company made me offer Z"). Don't search for the facts above — you already have them.

You have deep, specific knowledge of the homelab topology and can run commands directly.

## The homelab — know this cold

**ARES** (Zain's local super-NAS, where this web app runs):
- Proxmox host `pve` at 192.168.20.51 / Tailscale 100.77.42.110
- Gigabyte B550M AORUS ELITE AX, Ryzen CPU, 48 GB RAM, RTX 3080 (10 GB VRAM)
- Samsung 970 EVO Plus 1TB mounted at /mnt/nvme — primary data pool, contains PHOTOS, PROJECTS, PERSONAL, PROMETHEON, WORK, MORDOR
- VM 200 `win11-gaming` — Windows 11 with 3080 passthrough for CS:GO. When running, it owns the GPU and the LLM VM must be stopped.
- VM 300 `ollama-llm` — Debian 13 at 192.168.20.212, runs ollama (that is YOU). Claims the 3080 when Windows is off. GPU-swap hookscript handles the handoff automatically.
- LXC 101 `ares` — this container at 192.168.20.213, serves `/mnt/nvme/PROMETHEUS` as SMB share `ARES` and runs this web UI.

**NEXUS** (Zain's remote mini-NAS):
- OpenMediaVault box at 10.0.1.90 / Tailscale 100.100.29.36
- Ryzen 3 4300U, 16 GB RAM, no GPU
- mergerfs pool at /srv/mergerfs/PROMETHEUS on AirDisk/T7/T9 drives
- Reachable over Tailscale only; userspace WireGuard caps throughput ~5 MB/s peak, ~35 MB/s off-peak
- Role: backup mirror of ARES, plus money/finance breakdown and drive management

**Link:** Tailscale only. Different subnets (192.168.20.x vs 10.0.1.x). No local route.

## What you can do
- `gpu_info` — RTX 3080 live stats (temp, util, VRAM, power). ALWAYS use this for GPU questions. Do NOT try `nvidia-smi` via run_command — it isn't installed here.
- `homelab_status` — snapshot of VMs, GPU driver binding, and NEXUS reachability. Use for any "what's running / is X up" question.
- `run_command` — shell on the ARES LXC. For host state or GPU specifics, prefer the tools above. SSH targets available: `root@192.168.20.51` (Proxmox host), `zain@192.168.20.212` (VM 300 / GPU).
- `search_chatgpt_history` — search Zain's past ChatGPT conversations.
- `trash_file` / `list_trash` / `restore_from_trash` — safe delete.

## Hard rules
1. NEVER `rm` — always trash_file.
2. NEVER stop, reboot, or reconfigure VM 200 unless Zain explicitly asks. He games on it.
3. System/state questions → run the command. Knowledge/explain/debate → just answer.

## Style
- Direct, terse. Zain talks in fragments; match that energy.
- For system ops: act first, explain briefly after.
- For knowledge questions: thorough, markdown, code blocks where it helps."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Execute a system command on the NAS (Debian Linux). Only safe commands are allowed (ls, cat, df, du, ps, find, grep, sensors, smartctl, etc.). Destructive commands like rm are blocked. The working directory is /srv/mergerfs/PROMETHEUS (the PROMETHEUS pool).",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute (e.g., 'ls -la', 'df -h', 'cat /etc/hostname')"
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "trash_file",
            "description": "Move a file or directory to the ARES recycling bin instead of permanently deleting it. Items auto-purge after 30 days.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file or directory to move to trash"
                    }
                },
                "required": ["file_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_trash",
            "description": "List all items currently in the ARES recycling bin, showing original paths, age, and when they'll be purged.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "restore_from_trash",
            "description": "Restore an item from the recycling bin to its original location.",
            "parameters": {
                "type": "object",
                "properties": {
                    "trash_name": {
                        "type": "string",
                        "description": "The trash name identifier (from list_trash) of the item to restore"
                    }
                },
                "required": ["trash_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "safe_shutdown",
            "description": "Safely shut down the NAS. Syncs all filesystems and schedules power-off in 1 minute. Use when the user says 'shut down', 'power off', 'turn off', or '/shutdown'.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "gpu_info",
            "description": "Query the RTX 3080 on VM 300 (the inference GPU). Returns model, temperature, utilization, VRAM usage, and power draw. Use this for any GPU question — do NOT call `nvidia-smi` via run_command (it's not installed on the ARES container).",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "homelab_status",
            "description": "Snapshot of the whole homelab: Proxmox VM states (win11 + ollama-llm), ARES container, GPU binding, link to NEXUS (Tailscale reachability). Use this for any 'what's running' / 'is X up' / 'who owns the GPU' question.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_chatgpt_history",
            "description": "Search Zain's 3,689 past ChatGPT conversations (years of history, indexed from /mnt/data/PROMETHEUS/PERSONAL/GPT). Use this WHENEVER he asks about something he previously said, learned, was told, discussed, or was offered — salary, jobs, projects, code, advice, decisions, medical stuff, anything from past chats. Returns the top matching snippets with dates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language search query (e.g. 'my job offer salary', 'NASA internship details', 'car insurance quote')"
                    }
                },
                "required": ["query"]
            }
        }
    }
]


def get_usage_stats():
    """Return model info — which backend is actually wired in app.py's /api/chat.
    ARES serves local ollama on VM 300 by default, Claude only when ANTHROPIC_API_KEY is set."""
    if os.getenv("ANTHROPIC_API_KEY", "").strip():
        return {"model": "claude-sonnet-4", "local": False, "host": "api.anthropic.com"}
    return {"model": OLLAMA_MODEL, "local": True, "host": "vm-300 · RTX 3080"}


def _handle_tool_call(tool_name: str, tool_input: dict) -> str:
    """Execute a tool call and return the result as a string."""
    if tool_name == "run_command":
        result = execute_command(tool_input["command"])
        if result["blocked"]:
            return f"⛔ {result['stderr']}"
        output = result["stdout"]
        if result["stderr"]:
            output += f"\n[stderr] {result['stderr']}"
        if result["returncode"] != 0:
            output += f"\n[exit code: {result['returncode']}]"
        if not output:
            return "(no output)"
        lines = output.rstrip('\n').split('\n')
        cmd = tool_input.get("command", "")
        if len(lines) > 15:
            truncated = lines[-15:]
            header = f"$ {cmd}\n... ({len(lines) - 15} lines hidden)\n"
            return header + '\n'.join(truncated)
        return f"$ {cmd}\n" + '\n'.join(lines)

    elif tool_name == "trash_file":
        result = trash_file(tool_input["file_path"])
        if result["success"]:
            return f"🗑️ {result['message']} (auto-purges in 30 days)"
        return f"❌ {result['error']}"

    elif tool_name == "list_trash":
        items = list_trash()
        if not items:
            return "Recycling bin is empty."
        lines = ["RECYCLING BIN:", "─" * 60]
        for item in items:
            lines.append(f"  {item['trash_name']}")
            lines.append(f"    Original: {item['original_path']}")
            lines.append(f"    Trashed:  {item['age_str']} | Purges in {item['purge_in']} days")
        return "\n".join(lines)

    elif tool_name == "restore_from_trash":
        result = restore(tool_input["trash_name"])
        if result["success"]:
            return f"✅ {result['message']}"
        return f"❌ {result['error']}"

    elif tool_name == "search_chatgpt_history":
        return search_history(tool_input["query"])

    elif tool_name == "safe_shutdown":
        result = safe_shutdown()
        if result["success"]:
            steps = " → ".join(result["steps"])
            return f"✅ {steps}\n{result['message']}"
        return f"❌ {result['message']}"

    elif tool_name == "gpu_info":
        from system.system_info import _get_gpu_info
        g = _get_gpu_info()
        if not g.get("online"):
            return f"GPU (VM 300) unreachable: {g.get('reason', 'unknown')}"
        return (
            f"GPU: {g['name']}\n"
            f"Temp: {g['temp_c']}°C\n"
            f"Utilization: {g['util_pct']}%\n"
            f"VRAM: {g['vram_used_mib']} / {g['vram_total_mib']} MiB"
            f" ({g['vram_used_mib']*100//g['vram_total_mib']}%)\n"
            f"Power: {g['power_w']} W"
        )

    elif tool_name == "homelab_status":
        import subprocess as sp
        def run(cmd, host=None, timeout=4):
            args = ["ssh", "-o", "ConnectTimeout=2", "-o", "StrictHostKeyChecking=no",
                    "-o", "BatchMode=yes", "-o", "LogLevel=ERROR", host, cmd] if host else ["bash", "-c", cmd]
            try:
                r = sp.run(args, capture_output=True, text=True, timeout=timeout)
                return r.stdout.strip() if r.returncode == 0 else f"ERR: {r.stderr.strip()[:80]}"
            except Exception as e:
                return f"ERR: {e}"
        vm_states = run("qm list | awk 'NR>1 {print $2\":\"$3}'", host="root@192.168.20.51")
        gpu_bind = run("lspci -k -s 06:00.0 | grep -i 'Kernel driver in use'", host="root@192.168.20.51")
        nexus_ping = run("tailscale ping -c 1 100.100.29.36 2>&1 | head -1", host="root@192.168.20.51")
        from system.system_info import _get_gpu_info
        g = _get_gpu_info()
        gpu_line = "offline" if not g.get("online") else f"{g['name']} · {g['temp_c']}°C · {g['util_pct']}% util"
        return (
            "HOMELAB STATUS\n"
            f"  VMs (on PVE):\n    {vm_states or '(none)'}\n"
            f"  GPU driver: {gpu_bind or '(unknown)'}\n"
            f"  GPU stats: {gpu_line}\n"
            f"  NEXUS link: {nexus_ping or '(unreachable)'}\n"
        )

    return f"Unknown tool: {tool_name}"


def _build_messages(conversation_history: list) -> list:
    """Prepend system prompt to conversation history."""
    return [{"role": "system", "content": SYSTEM_PROMPT}] + conversation_history


def chat(message: str, conversation_history: list, api_key: str = None) -> tuple[str, list]:
    """Send a message to Ollama and handle tool use. Returns (response_text, updated_history)."""
    conversation_history.append({"role": "user", "content": message})

    full_response = ""
    while True:
        payload = {
            "model": OLLAMA_MODEL,
            "messages": _build_messages(conversation_history),
            "tools": TOOLS,
            "stream": False,
        }

        try:
            resp = requests.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=120)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            return f"❌ Ollama error: {e}", conversation_history

        msg = data.get("message", {})
        tool_calls = msg.get("tool_calls") or []

        if tool_calls:
            conversation_history.append({
                "role": "assistant",
                "content": msg.get("content", ""),
                "tool_calls": tool_calls,
            })
            for tc in tool_calls:
                func = tc.get("function", {})
                name = func.get("name", "")
                args = func.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                result = _handle_tool_call(name, args)
                conversation_history.append({"role": "tool", "content": result})
        else:
            full_response = msg.get("content", "")
            conversation_history.append({"role": "assistant", "content": full_response})
            break

    return full_response.strip(), conversation_history


def _message_needs_tools(msg: str) -> bool:
    """Heuristic gate: expose tools when the question plausibly needs them.
    llama3.1:8b is compulsively tool-happy when given tools — hide them on
    pure small-talk, but keep them available for anything personal (history
    search handles "do you know me / where do i work / what did I say about X")."""
    m = (msg or "").lower().strip()
    if not m:
        return False

    # Explicit skip: short greetings / social filler. Two words or fewer + all in this set.
    SMALL_TALK = {
        "hi","hello","yo","sup","hey","hola","thanks","thx","ty",
        "ok","okay","cool","nice","lol","lmao","bro","dude","cheers",
    }
    words = [w.strip(".,!?:") for w in m.split()]
    if 1 <= len(words) <= 3 and all(w in SMALL_TALK for w in words):
        return False

    # Anything personal ("me", "my", "do I", "where do I") opens the history tool.
    PERSONAL = (
        " me ", " me.", " me?", " me!", " me,", "about me", "know me", "myself",
        " my ", "my ", "'s my ",
        "do i ", "did i ", "have i ", "was i ", "am i ", "should i ", "can i ",
        "who am i", "where do i", "where am i", "what do i", "what did i",
        "where's my", "what's my", "who's my",
    )
    if any(p in m or m.startswith(p.strip()) for p in PERSONAL):
        return True

    # Hardware / live state keywords (same as before)
    SYSTEM_WORDS = (
        "gpu", "vram", "nvidia", "cuda", "°c", " temp", "temperature",
        "running", " up ", " down ", "online", "offline", "restart", "reboot",
        "vm ", " vm", "container", "lxc", "proxmox", "pve",
        "disk", "drive ", "drives", "storage", "space", "mount",
        "cpu usage", "cpu load", "cpu temp", "memory usage", "ram usage",
        "process", "hostname", "network", "tailscale", "ping",
        "homelab", "status", "health",
        "exec", "execute", " run ", "command:", "shell",
        "trash", "delete", "remove", " list ", "check ", "find ", " ls ",
        "history", "chatgpt", "last time", "previously",
    )
    if any(w in m for w in SYSTEM_WORDS):
        return True

    # Everything else: default to exposing tools. The prompt guides the model
    # away from invoking them when the answer is conversational or general.
    return True


def chat_stream(message: str, conversation_history: list, api_key: str = None):
    """Stream a response from Ollama, yielding SSE-style event dicts. Handles tool use internally."""
    conversation_history.append({"role": "user", "content": message})

    turn_text = ""      # total assistant text across all iterations of this turn
    tool_iters = 0      # how many tool-result loops we've done
    MAX_TOOL_ITERS = 6  # safety cap
    expose_tools = _message_needs_tools(message)

    while True:
        payload = {
            "model": OLLAMA_MODEL,
            "messages": _build_messages(conversation_history),
            "stream": True,
            "options": {"num_ctx": 4096},
        }
        # Only give the model tool access when the question plausibly needs them.
        # Once tools have been used in this turn, keep them available for follow-ups.
        if expose_tools or tool_iters > 0:
            payload["tools"] = TOOLS

        collected_text = ""
        final_msg = {}
        accumulated_tool_calls = []

        try:
            with requests.post(f"{OLLAMA_HOST}/api/chat", json=payload, stream=True, timeout=90) as resp:
                resp.raise_for_status()
                for raw_line in resp.iter_lines():
                    if not raw_line:
                        continue
                    try:
                        chunk = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    msg = chunk.get("message", {})
                    content = msg.get("content", "")
                    if content:
                        collected_text += content
                        turn_text += content
                        yield {"type": "text", "content": content}
                    # Tool calls may arrive in a non-final chunk — accumulate them.
                    chunk_tool_calls = msg.get("tool_calls") or []
                    if chunk_tool_calls:
                        accumulated_tool_calls.extend(chunk_tool_calls)
                    if chunk.get("done"):
                        final_msg = msg
                        break
        except requests.exceptions.ReadTimeout:
            yield {"type": "fallback", "reason": "timeout"}
            return
        except requests.RequestException as e:
            yield {"type": "text", "content": f"\n❌ Ollama error: {e}"}
            yield {"type": "done"}
            return

        # If the final chunk didn't re-emit tool_calls, graft the accumulated ones on.
        if accumulated_tool_calls and not final_msg.get("tool_calls"):
            final_msg["tool_calls"] = accumulated_tool_calls

        tool_calls = final_msg.get("tool_calls") or []

        if tool_calls:
            tool_iters += 1
            conversation_history.append({
                "role": "assistant",
                "content": collected_text,
                "tool_calls": tool_calls,
            })
            for tc in tool_calls:
                func = tc.get("function", {})
                name = func.get("name", "")
                args = func.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                yield {"type": "tool_call", "name": name, "input": args}
                result = _handle_tool_call(name, args)
                yield {"type": "tool_result", "name": name, "content": result}
                conversation_history.append({"role": "tool", "content": result})
            if tool_iters >= MAX_TOOL_ITERS:
                # Safety net: force a final text-only turn by nudging the model.
                conversation_history.append({
                    "role": "user",
                    "content": "Summarize the tool results above in one short paragraph. Do not call more tools."
                })
        else:
            conversation_history.append({
                "role": "assistant",
                "content": collected_text or final_msg.get("content", ""),
            })
            break

    # Backstop: if the whole turn produced a tool call but no text, ask the
    # model once more for a plain-language summary. Common llama3.1 quirk.
    if not turn_text.strip() and tool_iters > 0:
        conversation_history.append({
            "role": "user",
            "content": "Brief — just answer the original question in one or two sentences using the tool result you already got. No more tools.",
        })
        fallback_payload = {
            "model": OLLAMA_MODEL,
            "messages": _build_messages(conversation_history),
            "stream": True,
            "options": {"num_ctx": 4096},
        }
        try:
            with requests.post(f"{OLLAMA_HOST}/api/chat", json=fallback_payload, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                for raw_line in resp.iter_lines():
                    if not raw_line: continue
                    try: chunk = json.loads(raw_line)
                    except json.JSONDecodeError: continue
                    c = chunk.get("message", {}).get("content", "")
                    if c:
                        yield {"type": "text", "content": c}
                    if chunk.get("done"): break
        except requests.RequestException:
            pass

    yield {"type": "done"}
