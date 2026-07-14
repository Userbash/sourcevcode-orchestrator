# P2P Delivery And Worker Model

## When P2P is required

Use P2P delivery when the orchestrator must hand work to another agent instead of executing the task in-process. This is required when:
- routing selects a specialist agent;
- the first selected agent fails and the task must be rerouted;
- a plan contains parallel branches and each branch must be delivered independently;
- sub-agents need mailbox isolation, retry policy, and delivery telemetry.

## Delivery roles

- `Orchestrator`: producer and top-level controller.
- `Supervisor`: delivery control tower. Tracks every task envelope, handshake, retry count, timeout, and dead-letter transition.
- `Bus`: transport adapter. In-memory and RabbitMQ implementations share the same contract.
- `WorkerPool`: concurrent consumer group for one agent mailbox.
- `Agent`: business executor that actually performs the task.

## Envelope flow

1. `Dispatch` publishes `TaskEnvelope` to `agent.<agent_id>.inbox`.
2. Bus writes `sent` then `queued` to `ack_history`.
3. Worker fetches mailbox item.
4. `ConfirmPayload` validates checksum and writes `validated`.
5. `EstablishDelivery` completes handshake and writes `received`.
6. Worker executes handler.
7. On success, worker writes `accepted`.
8. On transient failure, worker writes `retrying` and requeues the envelope.
9. When retry budget is exhausted, envelope moves to dead-letter queue with `dead_lettered`.

## Status semantics

- `sent`: producer handed the message to the transport.
- `queued`: broker stored the envelope in the target mailbox.
- `received`: consumer picked up the envelope and delivery handshake is established.
- `validated`: payload checksum matches the original envelope.
- `retrying`: current execution attempt failed and the task was put back into a mailbox.
- `accepted`: task completed successfully.
- `dead_lettered`: delivery was terminated and moved to DLQ.
- `failed`: terminal execution failure without broker redelivery.

## Queue topology

- Agent mailbox: `agent.<agent_id>.inbox`
- Dead-letter queue: `dead_letter_queue`
- Delivery telemetry is exposed through supervisor snapshots and ack history.

## Postman control

The worker pool is the operational postman; the supervisor controls the postman.

Supervisor responsibilities:
- mailbox timeout inspection;
- retry budget enforcement;
- dead-letter transition;
- handshake and checksum tracking;
- health snapshots by agent.

Worker pool metrics:
- `processed`
- `succeeded`
- `failed`
- `retried`
- `dead_lettered`
- `validation_failures`
- `idle_polls`
- `active_workers`
- `average_latency_ms`
- `last_task_id`
- `last_error`

## Multitasking and multithreading

Run one worker pool per agent mailbox. Increase `concurrency` to allow multiple goroutines to consume the same mailbox in parallel. For plan execution, each plan task is dispatched independently and can be processed by a different worker at the same time. Failures remain local to the envelope and do not block sibling tasks.
