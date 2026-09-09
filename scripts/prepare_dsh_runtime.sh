#!/bin/bash
# Runs in a disposable preparation container, without API keys or task answers.
set -euo pipefail
mkdir -p /installed-agent /tmp/dsh-npm-cache
if [ ! -d /installed-agent/node ]; then
  python -c "import tarfile; from pathlib import Path; tarfile.open('/tmp/node.tar.xz').extractall('/installed-agent', filter='data'); Path('/installed-agent/node-v22.19.0-linux-x64').rename('/installed-agent/node')"
fi
if [ -d /tmp/npm-cache ] && [ ! -d /tmp/dsh-npm-cache/_cacache ]; then
  mv /tmp/npm-cache /tmp/dsh-npm-cache/_cacache
fi
export PATH=/installed-agent/node/bin:$PATH
packages=(
  @deepseek-ai/dsh@0.1.1-rc.2
  @deepseek-ai/cordis-plugin-group@1.0.2
  react@18.3.1 react-dom@18.3.1
)
for name in invariants scope fs atomic-write bash-local sandbox shell compaction workflow code-runtime timeout anonymous-user-id session-telemetry authorization output-retention session-title-llm spill subagent-in-process-driver; do
  packages+=("@deepseek-ai/dsh-${name}@0.1.1-rc.2")
done
npm install --prefix /installed-agent/dsh "${packages[@]}" \
  --legacy-peer-deps --cache /tmp/dsh-npm-cache --prefer-offline --no-audit --no-fund
DSH_HOME=/tmp/dsh-preflight-home /installed-agent/dsh/node_modules/.bin/dsh --profile headless --help
tar -czf /tmp/dsh-runtime.tgz -C /installed-agent node dsh
