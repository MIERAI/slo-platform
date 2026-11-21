#!/usr/bin/env bash
# Replays a recorded experiment run (docs/demo/sample-app-down.log: the real output of scripts/experiment.py on a fresh
# kind cluster, 2026-11-21) with its waits compressed — for the README's demo GIF, not a simulation of the tool.
cd "$(dirname "$0")"
pause() { case "$1" in *"takes effect"*) sleep 1.5;; *"fired after"*) sleep 3;; *"resolved"*) sleep 3;; *) sleep 0.6;; esac; }
printf '\033[2m# replay of a recorded run (kind, 2026-11-21) — waits compressed: 8 min shown in 20 s\033[0m\n'
while IFS= read -r line; do pause "$line"; printf '%s\n' "$line"; done < sample-app-down.log
