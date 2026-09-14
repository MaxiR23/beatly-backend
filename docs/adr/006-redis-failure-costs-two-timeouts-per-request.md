# A hung Redis costs two timeouts per request

## Context

The cache degrades rather than failing: when Redis does not answer, the
request goes to the provider and the failure is logged. What that costs
was not measured when the cache landed.

Measured locally against a host that silently drops packets, which is the
hung case rather than the refusing one:

- Healthy: 0.23 ms per read.
- Hung: 260 ms on the read, 254 ms on the write, 514 ms per request.

The socket timeout is 0.25 s and the client does not retry, so each
operation pays it once. A request pays it twice: the read fails, the
provider is called, and the write fails too.

A Redis that refuses connections is not this case: that fails
immediately.

## Decision

Accept the 514 ms. No circuit breaker.

## Consequences

A hung Redis adds half a second to every provider-backed endpoint, on top
of a provider call that already takes hundreds of milliseconds. Both
failures reach the log, so the condition is visible while it lasts.

Skipping the write after a failed read would halve it, and a circuit
breaker would remove it. Neither is added: a breaker carries state shared
across requests, and this service runs its sync handlers in a thread
pool, so that state is mutable and concurrent. That is a real source of
subtle bugs, introduced for a condition never observed in production.

The point of the log is that this does not have to be predicted. If those
warnings ever show up, the decision is revisited with the rate in hand
rather than with a guess.

Revisit if the warnings appear in production at all, or if the provider
endpoints stop being the slow part of a request.