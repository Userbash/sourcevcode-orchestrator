# SourceCraft Publication Report

## Overview

This release prepares SourceCraft for public-facing use inside the SourceVCode orchestration stack.

The work in this publication is not a cosmetic pass. It closes several runtime gaps that made the system harder to operate in real container environments, especially when work arrived through websocket clients or when transport-level delivery had to survive degraded infrastructure.

In plain terms, this release makes SourceCraft more predictable, easier to observe, and safer to run under imperfect host conditions.

## What SourceCraft Does

SourceCraft is the repository operations layer used by the orchestrator.

It is responsible for the part of the system that understands repository-oriented work such as:

- repository state inspection
- branch and pull request workflows
- release-oriented repository operations
- delegation of repository tasks into the wider orchestration runtime

SourceCraft does not replace the orchestrator. It gives the orchestrator a focused execution layer for repository workflows, while the orchestrator remains responsible for routing, scheduling, policy, memory, and multi-agent coordination.

## What Changed In This Release

### 1. Websocket sessions now behave like real user sessions

Before this release, websocket clients could fall into a shared default session identity. That made budgeting, provider state, and idempotency behavior bleed across unrelated websocket users.

This release changes that behavior by assigning a unique websocket session identity when the client does not provide one. As a result:

- one websocket client no longer consumes another client's session budget by accident
- idempotency is easier to reason about
- provider fallback state is isolated per websocket session
- websocket traffic no longer looks artificially lower priority because of a shared session bucket

### 2. Websocket routing hints are now explicit

Previously, websocket-specific intent could be partially lost between input handling and task creation.

This release carries websocket metadata into the task model in a structured way. The runtime now preserves and uses:

- source
- provider preference
- requested model
- cost tier
- websocket channel markers
- optional complexity hints

That change matters because the system can now make routing decisions based on what the websocket client actually asked for, rather than inferring everything from generic task shape.

### 3. Cost and provider routing are easier to understand

This release introduces a cleaner policy for websocket-driven work.

Instead of leaving websocket tasks to inherit whatever fallback order happened to apply, the routing layer now distinguishes between interactive, economy, and premium behavior. In practice, that means:

- economy websocket work can prefer cheaper providers and lighter OpenAI models
- premium websocket work can prefer stronger models when appropriate
- explicit provider preference from the client is honored first when valid
- session-level OpenAI budgeting can be tuned separately for websocket traffic

This gives operators a usable control surface. They can now shape websocket cost and responsiveness without rewriting the main routing logic.

### 4. SourceCraft startup is safer under degraded host conditions

One of the most important fixes in this release is startup resilience.

SourceCraft used to contribute to hard boot failures when host-side tools such as distrobox were unavailable from inside the container path. That kind of failure is too expensive because it can take down the entire orchestrator path for a feature that should degrade gracefully.

This release changes the behavior so that SourceCraft can move into a degraded state instead of forcing a fatal boot path. The runtime can now stay online while clearly reporting that SourceCraft is partially unavailable.

This is the right tradeoff for production-style operation: core orchestration stays alive, repository tooling reports its state honestly, and operators can recover the bridge separately.

### 5. Message transport serialization is more robust

A transport bug in the RabbitMQ delivery path could break task delivery when envelope-like objects were serialized with the wrong assumptions.

This release hardens transport serialization so that orchestration envelopes and peer-to-peer messages are serialized consistently through the bus layer.

That matters because a routing fix is not complete if the delivery layer still drops the task. This publication closes that gap.

### 6. Streaming behavior is friendlier for long-running websocket tasks

This release adds heartbeat-style streaming behavior during websocket task execution.

The goal is simple: long-running tasks should not appear dead just because they are quiet for a while. Operators and clients now have a better signal that the task is still active even when the downstream model is slow.

## New Components And Additions

This publication also introduces or formalizes several supporting pieces around the runtime:

- cache guard logic for safer repeated-session behavior
- prompt serialization support for more stable prompt shaping
- websocket test helpers and regression coverage
- publication and cost-planning documentation for rollout and future tuning

These additions are important because they reduce operational ambiguity. The system now has better internal boundaries between routing, transport, session behavior, and serialization.

## Files And Areas Most Affected

The largest changes in this release center around these areas:

- `core/scripts/orchestrator_daemon.py`
- `core/core/task_submission_api.py`
- `core/core/provider_budget_router.py`
- `core/core/openai_runtime_router.py`
- `core/core/orchestrator.py`
- `core/core/sourcecraft_module.py`
- `core/core/rabbitmq_bus.py`

Together, these files define how websocket tasks enter the system, become structured work, receive cost and provider policy, survive degraded startup conditions, and reach downstream execution.

## Why This Release Matters

This publication makes SourceCraft meaningfully more usable in a real deployment.

The main improvements are not about adding a flashy new command. They are about making the system trustworthy:

- websocket clients now behave like first-class clients
- routing policy is more explicit
- cost control is more practical
- degraded infrastructure no longer causes unnecessary full-runtime failure
- delivery is more reliable
- documentation now explains the intent in human terms

That combination is what turns an internal feature set into something that is ready to present, test, and iterate on with confidence.

## Remaining Operational Notes

A few environment-specific issues are still operational concerns rather than publication blockers:

- OpenAI availability still depends on a valid `OPENAI_API_KEY`
- local LLM responsiveness still depends on the host-side model endpoint staying healthy
- full SourceCraft green status still depends on the host bridge path that provides distrobox-backed execution where required

Those are runtime environment concerns, not structural release blockers for the publication itself.

## Summary

This SourceCraft publication is a stability and clarity release.

It improves session handling, routing fidelity, transport safety, degraded startup behavior, and operator-facing documentation. The result is a SourceCraft runtime that is easier to explain, easier to run, and less likely to fail in confusing ways under real workload conditions.
