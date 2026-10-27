# Rhymax Mower (Home Assistant)

Cloud Home Assistant integration for Rhymax / RoboUP robotic mowers (X9 / X10).

**Status:** scaffold — cloud OAuth + lawn_mower entities coming as gateway HA fulfillment lands.

## Install (HACS)

1. HACS → Integrations → ⋮ → Custom repositories
2. Add this repo as Integration
3. Install **Rhymax Mower**, restart Home Assistant

## Capabilities (target)

| Capability | MVP |
|---|---|
| Start / pause / resume / dock | yes |
| Zone mow | yes |
| Progress % | yes |
| Owner devices only | yes |
| Local LAN control | later |
| Device schedule editor | later |
| User webhooks | later |

## Auth

OAuth against RoboUP cloud (`smarthome` scope). Never uses device tokens, MCP, or factory brokers.

## License

MIT
