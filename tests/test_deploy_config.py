from pathlib import Path

text = Path("docker-compose.yml").read_text(encoding="utf-8")

if "build:" not in text or "context: ." not in text:
    raise SystemExit("docker-compose.yml should build grok2api from this repository by default")

if "ghcr.io/chenyme/grok2api:latest" in text:
    raise SystemExit("docker-compose.yml should not default to the upstream image")

print("deploy_config_check=ok")
