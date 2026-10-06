---
id: tic-6c05
title: 'docs overview: list the topics inline (tic-ae66 follow-up)'
status: closed
type: request
tier: low
domain: docs
epic: developer-docs
priority: 3
tags:
- docs
- dx
assignee: deepseek.flash.001
depends_on: []
blocked_by: null
created: '2026-10-06T11:29:16'
updated: '2026-10-06T11:29:22'
closed: '2026-10-06T11:29:22'
---

## Description
The 'arbite docs' overview now lists the topics inline (rendered from the TOPICS registry) so an agent does not need a separate 'arbite docs list' lookup; list/all/search/-h remain. Overview stays 44 lines / 2.8 KB, under the budget.

## Notes
- 2026-10-06T11:29:22 deepseek.flash.001: The 'arbite docs' overview now carries a '## Topics' section listing every topic (name + summary, rendered from the TOPICS registry so it cannot drift), the reserved 'commands [NAME]', and a one-line pointer to list/all/search/-h. Budget test still passes (44 lines / 2787 bytes vs 60/4500). Observable: run 'arbite docs' and the topic menu is right there; 'arbite docs list' still prints the same topics grouped by section.

- 2026-10-06T11:29:22 system: Submitted; closed (review disabled).
