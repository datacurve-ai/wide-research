# Preset blocks

Paste into your TOML job config. All `[resources]` values are **minimum
requests** — containers burst above when the worker has spare capacity.

## Research (default)

Light web lookups, file summaries, small Python scripts. Cheap, fast,
enough for most wide-search jobs.

```toml
[resources]
cpu = 0.25
memory = 512

[image]
apt_packages = ["curl", "ca-certificates"]
```

## Coding

`pytest`, `ruff`, small builds, data processing, git operations. Bigger
budget so test suites don't OOM.

```toml
[resources]
cpu = 2.0
memory = 4096

[image]
apt_packages = ["git", "build-essential", "curl", "ca-certificates"]
pip_packages = ["pytest", "ruff"]
```

## Docker (DinD)

Container builds, `docker run …` inside the sandbox. `enable_docker = true`
splices in `docker.io`, the iptables-legacy switch, and a dockerd entrypoint.

```toml
[resources]
cpu = 4.0
memory = 8192

[image]
enable_docker = true
apt_packages = ["curl", "ca-certificates"]
```

## GPU

Inference, small training jobs. Pick the smallest that fits the weights.

```toml
[resources]
cpu = 4.0
memory = 16384
gpu = "T4"        # or "A10G", "L4", "A100", "H100:8", etc.

[image]
pip_packages = ["torch"]
```

## Headless browser

Not a built-in preset — but the pattern is the same as coding + a few apt
packages.

```toml
[resources]
cpu = 2.0
memory = 4096

[image]
apt_packages = [
    "chromium", "chromium-driver",
    "fonts-liberation", "libnss3", "libatk1.0-0", "libatk-bridge2.0-0",
    "libcups2", "libxkbcommon0", "libxcomposite1", "libxdamage1",
    "libxrandr2", "libgbm1", "libpango-1.0-0", "libcairo2",
]
pip_packages = ["playwright"]
run_commands = ["playwright install chromium"]
```
