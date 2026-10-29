# Rhymax Mower for Home Assistant

Control your Rhymax / RoboUP robotic mowers from Home Assistant. The integration links your RoboUP account and talks to the RoboUP cloud.

Powered by Rhymax Cloud.

## Requirements

- Home Assistant 2025.4 or newer.
- A RoboUP account (the one you use in the RoboUP app) that owns at least one mower. Mowers shared with you by another account are not listed.
- The OAuth client secret that Rhymax issues for Home Assistant linking (see [Add Application Credentials](#1-add-application-credentials)).

## Installation

### HACS (custom repository)

1. In Home Assistant, open **HACS**.
2. Open the ⋮ menu in the top right corner and select **Custom repositories**.
3. Enter `https://github.com/rhymax-labs/ha-rhymax-mower`, choose the type **Integration** and select **Add**.
4. Search for **Rhymax Mower**, open it and select **Download**.
5. Restart Home Assistant.

### Manual

Copy the `custom_components/rhymax_mower` folder into the `custom_components` folder of your Home Assistant configuration directory, then restart Home Assistant.

## Setup

### 1. Add Application Credentials

Home Assistant signs in to the RoboUP cloud with OAuth Application Credentials that Rhymax issues for Home Assistant linking.

1. Go to **Settings → Devices & services**, open the ⋮ menu and select **Application credentials**, then **Add application credential**.
2. Fill in:

   | Field | Value |
   |---|---|
   | Integration | Rhymax Mower |
   | Name | Any name, for example `RoboUP` |
   | OAuth client ID | `homeassistant` |
   | OAuth client secret | The client secret issued by Rhymax for Home Assistant linking |

If you skip this step, Home Assistant asks for the credentials when you add the integration.

### 2. Add the integration

1. Go to **Settings → Devices & services → Add integration** and choose **Rhymax Mower**.
2. A RoboUP sign-in page opens. Sign in the same way you do in the RoboUP app (email and password, or Google) to allow Home Assistant access.
3. You return to Home Assistant, and every mower on your account is added as a device.

### Redirect URL

After you sign in, the RoboUP cloud returns you to Home Assistant through [My Home Assistant](https://my.home-assistant.io/):

```text
https://my.home-assistant.io/redirect/oauth
```

The integration always uses this redirect URL, and there is nothing to configure for it on the RoboUP side. The first time you use My Home Assistant, it asks for the address of your Home Assistant instance; make sure it points to the instance you are setting up.

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

The push implementation is experimental on this local branch. Cloud WebSocket acceptance testing is required before merging or distributing the behavior change. See [implementation and testing notes](docs/ha-scoped-ws.md).

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
| Local (LAN) control | No |
| Mowing schedule editing | No |
| Cloud status updates | WebSocket push with REST fallback; requires the HA-scoped cloud endpoint |

## Removing the integration

Deleting the integration also unlinks Home Assistant from your RoboUP account. Afterwards you can delete the Application Credentials as well.

A mower removed from your RoboUP account is removed from this integration's entities at the next device-list reconciliation. Reloading the integration closes its local connection without disconnecting the account.

## Troubleshooting

- **"Home Assistant linking is not available yet"**: linking Home Assistant to the RoboUP cloud is not available yet. Please try again later.
- **"Rhymax cloud did not accept the Application Credentials"**: check that the client ID is exactly `homeassistant` and that the client secret is correct (**Settings → Devices & services → ⋮ → Application credentials**). If both are correct, Home Assistant linking is not available yet.
- **The sign-in page does not return to your Home Assistant**: My Home Assistant points to a different address. Update your Home Assistant address at [my.home-assistant.io](https://my.home-assistant.io/) and try again.
- **Home Assistant asks you to sign in to RoboUP again**: the link to your RoboUP account expired or was removed. Open the notification in **Settings → Devices & services** and sign in with the same RoboUP account.
- **A mower is missing**: only mowers that your account owns are added. New mowers appear after the next successful device-list reconciliation (normally checked every 60 seconds).

## Privacy

The integration only talks to the RoboUP cloud. Home Assistant can read your mower list and mower status, and send the commands listed above. The sign-in page shows what is shared with Home Assistant.

## License

MIT
