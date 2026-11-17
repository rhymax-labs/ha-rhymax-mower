# Rhymax Mower for Home Assistant

Control your RHYMAX robotic mowers from Home Assistant. The integration links your RHYMAX account using OAuth2 with PKCE and talks directly to the RHYMAX Cloud—no client secret or manual credential entry required.

Powered by RHYMAX Cloud.

## Requirements

- Home Assistant 2025.4 or newer.
- A RHYMAX account (the one you use in the RHYMAX App) that owns at least one mower. Mowers shared with you by another account are not listed.

## Installation

### One-click via My Home Assistant (HACS)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=rhymax-labs&repository=ha-rhymax-mower&category=integration)

Or tap **Open in HACS** inside the RHYMAX App (**Settings → Smart Home / Voice Control → Home Assistant**), then select **Download** and restart Home Assistant.

### Manual HACS custom repository

1. In Home Assistant, open **HACS**.
2. Open the ⋮ menu in the top right corner and select **Custom repositories**.
3. Enter `https://github.com/rhymax-labs/ha-rhymax-mower`, choose the type **Integration** and select **Add**.
4. Search for **Rhymax Mower**, open it and select **Download**.
5. Restart Home Assistant.

### Manual file install

Copy the `custom_components/rhymax_mower` folder into the `custom_components` folder of your Home Assistant configuration directory, then restart Home Assistant.

## Setup

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=rhymax_mower)

1. Go to **Settings → Devices & services → Add integration** and choose **Rhymax Mower** (or tap **Add Integration in Home Assistant** in the RHYMAX App).
2. A RHYMAX sign-in page opens automatically—no Application Credentials or client secret needed. Sign in the same way you do in the RHYMAX App (email and password, or Google) to allow Home Assistant access.
3. You return to Home Assistant, and every mower on your account is added as a device.

### Redirect URL

After you sign in, RHYMAX Cloud returns you to Home Assistant through [My Home Assistant](https://my.home-assistant.io/):

```text
https://my.home-assistant.io/redirect/oauth
```

The integration uses OAuth2 Authorization Code + PKCE (`S256`) with this redirect URL, and there is nothing to configure manually. The first time you use My Home Assistant, it asks for the address of your Home Assistant instance; make sure it points to the instance you are setting up.

## Entities

Each mower is a device with these entities:

| Entity | Description |
|---|---|
| Lawn mower | Current activity, and the controls: start mowing (resumes a paused task), pause, return to dock, and stop (Home Assistant 2026.10 or newer) |
| Battery | Battery level in % |
| Mowing progress | Progress of the current or most recent mowing task in % |
| Charging | Whether the mower is charging |
| Connectivity | Whether the mower is online (diagnostic) |

This branch adds cloud WebSocket updates alongside permanent REST reconciliation. While push is connected, an independent watchdog checks every mower and reconciles a quiet mower after 90 seconds by default. If push is unavailable, REST checks continue at roughly 30-second intervals. These are fallback defaults, not delivery guarantees. A quiet connection never makes a mower offline: Connectivity follows the cloud's reported `online` value.

Commands are confirmed through subsequent state updates, with up to three REST confirmation reads within 30 seconds using the same request budget as the watchdog. A successful command response means it was accepted; it does not guarantee that the mower has completed it. While a mower is reported offline, its controls and readings are unavailable and Connectivity shows it as disconnected.

See [implementation and testing notes](docs/ha-scoped-ws.md).

### Lawn mower activity

| Mower | Shown in Home Assistant as |
|---|---|
| Mowing | Mowing |
| Paused | Paused |
| Returning to the dock | Returning |
| On the dock, including while charging or waiting for rain to stop | Docked |
| Error or emergency stop | Error |
| Standing still away from the dock, or busy with something other than mowing (such as mapping) | Idle (Paused before Home Assistant 2026.10) |

## Capabilities

| Capability | Supported |
|---|---|
| Start, resume, pause, return to dock | Yes |
| Stop | Yes, on Home Assistant 2026.10 or newer |
| Battery level, charging, mowing progress | Yes |
| Zone mowing | No (not available) |
| Mowers shared with you by another account | No (only mowers your account owns) |
| Local (LAN) control | No (legacy local MQTT remains in RHYMAX App → Home Assistant → Advanced) |
| Mowing schedule editing | No |
| Cloud status updates | WebSocket push with REST fallback; requires the HA-scoped cloud endpoint |

## Removing the integration

Deleting the integration also unlinks Home Assistant from your RHYMAX account.

A mower removed from your RHYMAX account is removed from this integration's entities at the next device-list reconciliation. Reloading the integration closes its local connection without disconnecting the account.

## Troubleshooting

- **"Home Assistant linking is not available yet"**: linking Home Assistant to RHYMAX Cloud is not available yet. Please try again later.
- **The sign-in page does not return to your Home Assistant**: My Home Assistant points to a different address. Update your Home Assistant address at [my.home-assistant.io](https://my.home-assistant.io/) and try again.
- **Home Assistant asks you to sign in to RHYMAX again**: the link to your RHYMAX account expired or was removed. Open the notification in **Settings → Devices & services** and sign in with the same RHYMAX account.
- **A mower is missing**: only mowers that your account owns are added. New mowers appear after the next successful device-list reconciliation (normally checked every 60 seconds).

## Privacy

The integration only talks to RHYMAX Cloud. Home Assistant can read your mower list and mower status, and send the commands listed above. The sign-in page shows what is shared with Home Assistant.

## License

MIT
