import os, sys, re
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "autofix"))
import apply
import audit

_DEAD_LAN = re.compile(r"\b10\.0\.1\.\d+\b")


def test_eros_ssh_target_uses_tailnet_alias_not_dead_lan_ip():
    # 2026-09-28: root@10.0.1.69 timed out, so the audit could not inspect EROS
    # and EROS command fixes could not apply. The `eros` alias rides tailscale.
    assert apply.SSH_TARGETS["EROS"] == "eros"


def test_audit_prompt_has_no_dead_lan_address():
    assert not _DEAD_LAN.search(audit.PROMPT)
    assert "ssh eros" in audit.PROMPT


def test_pipeline_code_has_no_private_ip_literals_except_host_bridge():
    # Hardcoded LAN IPs have broken this pipeline twice (10.0.1.x after the
    # LAN move, VM 300's .212). Reach other boxes via ssh aliases; the only
    # allowed literal is the ARES host's own vmbr0 bridge, which LXC 101 uses.
    allowed = {"192.168.20.51"}
    private = re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){2,3}\b")
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "autofix")
    hits = []
    for name in sorted(os.listdir(here)):
        if not name.endswith((".py", ".sh")):
            continue
        with open(os.path.join(here, name)) as f:
            for n, line in enumerate(f, 1):
                hits += [f"{name}:{n} {ip}" for ip in private.findall(line) if ip not in allowed]
    assert hits == []
