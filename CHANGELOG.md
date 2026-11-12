# Changelog

Version numbers follow the `major.minor.patch` form (e.g. 1.2.3).

## 0.1.0 — 2026-11-13

First public version.

**Launcher**
- Launch sequence: starts apps in order with a wait between each, without starting running apps twice. A timeline under the apps table shows when each app starts
- Status lamps for running / administrator / mismatched rights, and stream / recording status on the OBS tile
- "Lock while OBS runs" per app (on by default): while OBS is running, clicking the icon doesn't open the app or page, so a stray click can't show up on stream. The icon's right-click menu still opens it
- Display: "Icon only" (expands on mouse-over) or "Always expanded"; GROUP layout with optional limits down / across; categories that slide out sideways
- Collapsed icon can show one of your registered apps' icons; wheel rotation of the icons can be turned off
- Tile icons from an image file, the exe, or a web address (a site's icon, or an image's address)

**D&D mode**
- Add image, video and audio sources to OBS over obs-websocket, even when OBS runs as administrator
- The D&D space opens next to the launcher without covering it. Drag it to move, drag its striped corner to resize; position and size are remembered
- "Always on top" can be set separately for the launcher and the D&D space

**Look**
- Color editor: opacity slider, custom colors with color codes, and six templates (Amber, Monochrome, Sakura, Forest, Light, High contrast). Changes show on the launcher right away; Cancel puts them back

**Settings**
- Apps table: alternating row colors, the app name stays visible while you scroll sideways, columns can be moved and resized, and a removed row comes back with "Undo" (Ctrl+Z)
- "Wait (s)" accepts only numbers (full-width digits become half-width)
- The mouse wheel doesn't change numbers or drop-downs you haven't clicked
- Ports: "Detect" adds the ports your apps use (including OneComme, BouyomiChan and Yukarinette Connector NEO) and warns about conflicts; a note for whether this PC's IP address is static or dynamic, with a check that reads only the DHCP setting (the IP itself is never read)
- UI in English / 日本語 / 한국어 / Bahasa Indonesia / Français / 繁體中文 ("Auto" follows OBS's language)
- Info tab with version, build date and developer credit, plus Debug Mode
- Settings export / import, all at once or per item, with automatic backups of the previous settings
- Settings stored in `%APPDATA%\CBApps\DDLauncher\`, copied over automatically from the old `%APPDATA%\DDLauncher\`

**Install / uninstall**
- Installer and standalone (one folder) versions
- Safe uninstaller (`DDLauncher-Uninstall.exe`): export settings first, review what it found, and move only the checked items to the Recycle Bin
