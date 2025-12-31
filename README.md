# Home Assistant Neptune Apex Monitor

This custom integration pulls real-time probe data from a [Neptune Apex](https://www.neptunesystems.com/) 
and makes it available in Home Assistant as sensors, read-only controls, and diagnostics.

## Features
- Hardware, software, firmware info
- Auto adds all probes and outlets
  - Outles are currently read only 
- Diagnostic using amp and last power
- Energy dashboard support via "Apex Total Energy"
- Parses local status.xml from Apex Controller
- Configurable F/C for temp
- Works only on the local network
- Designed for HACS install

## Installation
### Installation via HACS
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=denov&repository=home-assistant-apex-neptune&category=Integration)
2. Use HACS to install the integration.
3. Restart Home Assistant.
4. Set up the integration using the UI:

<a href="https://my.home-assistant.io/redirect/config_flow_start/?domain=apex" target="_blank" rel="noreferrer noopener">
 <img src="https://my.home-assistant.io/badges/config_flow_start.svg" alt="Add Integration">
</a>

### Manual Installation

1. Download the integration files from the GitHub repository.
2. Place the integration folder in the custom_components directory of Home Assistant.
3. Restart Home Assistant.
4. Set up the integration using the UI:

## Future
- Enable outlet change


## License

This project is licensed under the **GNU Affero General Public License v3.0 or later**
(**AGPL-3.0-or-later**). See `LICENSE`. 

### Commercial use
Commercial use is allowed under AGPL if you comply with its requirements (including
providing corresponding source to users who interact with the software over a network). :contentReference[oaicite:4]{index=4}

If you want to use this project without AGPL obligations, a **commercial license**
is available—see `COMMERCIAL_LICENSE.md`.
