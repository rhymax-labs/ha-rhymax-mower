# HA-scoped WebSocket client

This branch implements the public HA WebSocket contract alongside the existing
OAuth and REST client. It is local development work; cloud acceptance is still a
gate before merging or publishing. The integration remains `cloud_polling`.

## Transport and state

- Connect to `wss://www.cloud.iroboup.com/api/v1/homeassistant/ws` with
  `Authorization: Bearer <access>`, using the same OAuth session as REST. No query
  credential, first-frame credential or subprotocol is used.
- REST list, state, commands and disconnect remain under
  `/api/v1/homeassistant/*`. No App status API is used.
- Both transports parse the ten frozen HaState fields. Invalid presence is an
  error; an offline state clears all seven dynamic fields. Unknown activities
  become `other`. App extensions are ignored.
- A local socket epoch rejects callbacks from an old connection. Each connection
  separately tracks `subscription_generation`; lower generations are dropped,
  and a new connection can start again at generation 1. Event IDs are opaque
  strings, with bounded equality-based deduplication and no ordering assumption.
- Native ping/pong keeps the socket alive. Heartbeats update only the channel
  clock, never a mower's state freshness. Acknowledged heartbeats with no state
  changes are valid and do not require reconnecting.

The client assumes best-effort snapshots and changes, with no replay or specific
gateway worker. It does not depend on the Google/Alexa reporter pipeline. The
aligned gateway MVP uses per-connection TelemetryManager subscriptions and may
omit `device_list_changed`; independent REST list reconciliation is always kept.

## Reconciliation and commands

The DataUpdateCoordinator has no scheduled poll interval. A separate background
watchdog wakes every 5 seconds and schedules due work without waiting for other
mowers' requests. `async_set_updated_data()` cannot postpone this timer.

Each mower separately records its last valid push, last HTTP state attempt and
last successful HTTP state response. Connected push uses a 90-second quiet
threshold; disconnected push uses a 30-second REST interval. The device list is
reconciled every 60 seconds even if state pushes never stop. These values are
constructor-configurable defaults (`RhymaxTimings`), not UI options or latency
guarantees.

All reads for a mower share one in-flight request and attempt budget. Attempts are
charged before HTTP starts, with a 5-second minimum between starts; failures back
off from 30 seconds to 300 seconds. Numeric `Retry-After` is respected within the
API's bounded parsing policy. Commands get at most three confirmation reads in a
30-second window. New confirmations replace older ones for the same mower and
can finish early on a matching push. Budget exhaustion does not imply command
failure or device offline; normal reconciliation continues.

State reads capture a per-device revision before I/O. Responses that race a new
push or refer to an older telemetry timestamp cannot overwrite current state.
Removing and re-adding a mower also invalidates its earlier requests. Inventory
pushes fence in-flight REST lists. Unknown mower pushes request bounded list
reconciliation instead of inventing metadata. Inventory updates add, rename and
remove entities, while preserving registry links owned by other config entries.

## Authentication and lifecycle

`4401` and `1008` fence the rejected access token, refresh through the shared
OAuth session and open a new socket. A permanent refresh failure or immediate
rejection of the renewed credential stops traffic and starts HA reauthentication.
Transient refresh failures retry refresh with backoff, without reconnecting the
rejected token. `4403` stops the subscription; REST remains available. `4502` and
network failures use exponential backoff with jitter, capped at 60 seconds.

The live socket also checks for token rotation every 30 seconds; rotation closes
the previous socket before opening its replacement. Concurrent REST/WS token
refreshes share a lock to protect rotating refresh tokens.

Setup uses REST first and starts background work after platforms are loaded.
Unload cancels socket, watchdog, reconnect, confirmation and request tasks. Entry
deletion additionally calls the existing HA `disconnect` endpoint. Ordinary
reload/unload does not call account-wide disconnect, which would revoke the
credentials used by this entry and other HA instances.

## Tests

There were no tests in the v0.1.0 baseline. The added suite uses the real Home
Assistant pytest plugin, with cloud REST and WebSocket I/O mocked. The pinned
development environment is Python 3.14 and Home Assistant 2026.10.4:

```sh
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/ruff check custom_components tests
git diff --check
```

The suite covers frozen payloads, endpoint/auth headers, generation reset and
late callbacks, close-code recovery, heartbeat handling, per-device starvation,
REST/push races, failure/429 budgets, finite command confirmation, dynamic
inventory and shutdown. Live gateway credential isolation, cross-worker revoke
latency, multiple real HA instances and production TCP behavior still require
cloud acceptance testing. No cloud deployment or publishing is part of this
implementation.
