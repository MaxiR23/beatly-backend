# 001. Provider errors import direction

Why `core/upstream.py` imports `PROVIDER_ERRORS` from
`core/search_provider.py`, and why that dependency is not
inverted.

## Context

`translate_upstream_errors()` is the single place that maps a
provider failure to `upstream_error` or `upstream_timeout`. The
external provider raises its own error classes, which the
translator has to catch.

## Decision

The provider module exports `PROVIDER_ERRORS`, a tuple of error
classes, and the translator imports it.

## Consequences

Why the alternatives were rejected:

- The direction is forced by `except`, not chosen. An
  `except (A, B)` needs the class objects when it evaluates, so
  the translator has to name what it catches.
- Inverting it means the provider client raising `UpstreamError`
  itself. `UpstreamError` and `UpstreamTimeout` are `AppError`
  subclasses and each fixes a status code, so that puts an HTTP
  decision in a provider client, the same category of mistake as
  a service importing `HTTPException`. It would also split the
  504-vs-502 criterion, currently in one documented place, across
  every client.
- A registration function would invert the import but make the
  caught set mutable and dependent on import order, which is
  worse than the coupling it avoids.
- A module-level import of `core.upstream` from
  `core.search_provider` closes a literal cycle.

What is accepted: the external library loads when any service is
imported, including the seven domains that do not use it. And
`PROVIDER_ERRORS` holds two classes from one provider today.
Adding tracks, artists, album and audio must not add four more
imports to `core/upstream.py`: revisit the seam before the fourth
domain lands.
