---
id: tic-a583
title: Parse YAML with the libyaml C loader, falling back to the pure-Python one
status: closed
type: request
tier: low
domain: io
epic: milieu
priority: null
tags: []
assignee: claude.opus-5-5.001
depends_on: []
blocked_by: null
created: '2026-10-10T09:42:27'
updated: '2026-10-10T09:54:04'
closed: '2026-10-10T09:54:04'
---

## Description
Reading many project states is dominated by parsing ticket frontmatter. Use yaml.CSafeLoader when PyYAML was built with libyaml, and fall back to yaml.SafeLoader otherwise. Writing keeps the pure-Python dumper so stored ticket text stays byte-for-byte what it is today.

## Notes
- 2026-10-10T09:54:03 claude.opus-5-5.001: schema.load_yaml parses through yaml.CSafeLoader when PyYAML has libyaml and yaml.SafeLoader otherwise; parse_ticket and config loading use it. Dumping is unchanged (pure Python) so stored text stays byte-stable.

Observable: no change in any command's output or exit code; listings over large file-sink stores are faster (frontmatter parsing measured about 8x faster on this repo's 64 closed tickets). On an install whose PyYAML lacks libyaml everything behaves as before.

- 2026-10-10T09:54:04 system: Submitted; closed (review disabled).
