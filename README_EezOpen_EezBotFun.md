# EezOpen v1.2.0

EezOpen is an open-source Linux companion for the **EezBotFun 8-Key MacroPad**.

It provides a Linux-native way to configure and use the device with a small user-session daemon and a GTK4 configurator. The daemon owns the MacroPad serial connection; the configurator talks to the daemon through a local Unix socket.

## Current architecture

EezOpen has two runtime components:

### `eezopen-daemon`

The daemon runs as the logged-in user and is responsible for:

- detecting the MacroPad;
- owning the CDC serial port;
- device authentication;
- receiving key events;
- executing host-side Non-HID actions;
- launching **External Script** actions;
- applying profile, RGB and key state over serial;
- writing individual configuration/icon files when requested;
- serving a local IPC API to the GTK configurator;
- sending PC Monitor data;
- temporarily handing the CDC serial port to a Configurator-managed **Screen Script**;
- firmware-update orchestration.

The daemon does **not** recursively scan or mirror the MacroPad USB Mass Storage merely because the device connected.

### `eezopen-config`

The GTK4 configurator is responsible for the user-facing configuration workflow:

- profile selection and profile count;
- HID and Non-HID key editing;
- External Script configuration;
- one icon per key;
- RGB controls;
- firmware update;
- custom background install/removal;
- selecting and starting/stopping a long-running Screen Script;
- explicit synchronization with MacroPad USB Mass Storage.

The configurator never opens the serial port directly. It sends IPC requests to the daemon.

## Local IPC

The daemon socket is created under:

```text
$XDG_RUNTIME_DIR/eezopen/daemon.sock
```

The runtime directory is mode `0700` and the socket is mode `0600`.

## USB personalities

Observed EezBotFun USB identities:

```text
303a:4007  Normal runtime: CDC + HID + USB Mass Storage
303a:4005  Safe Mode: CDC + HID, no USB Mass Storage
303a:0012  Recovery/updater personality
```

The normal runtime detector accepts the known runtime identities and ignores the updater/recovery personality.

## Serial access

EezOpen uses the normal Linux serial permission model.

The expected group is:

```text
dialout
```

The installer does not install an EezOpen-specific USB authorization/unbind rule and does not force USB re-enumeration.

If the installer adds your user to `dialout`, log out and back in before starting the user service so the graphical session and systemd user manager inherit the new supplementary group.

For a temporary foreground shell only:

```bash
newgrp dialout
```

## Persistent local cache

Per-device data is stored under:

```text
~/.local/share/EezOpen/
```

Main directories:

```text
configs/<device-id>/
scripts/<device-id>/
app_icons/<device-id>/
assets/
```

The persistent cache survives daemon restarts, device disconnects and reboots.

The daemon uses this persistent cache for host-side key actions. Connecting the MacroPad does not automatically replace it.

## Mass Storage policy

USB Mass Storage is intentionally kept out of the daemon's normal connection loop.

When the MacroPad connects, the daemon establishes CDC/serial operation and uses the persistent local cache. It does not recursively read:

```text
configs/
scripts/
app_icons/
```

Recursive Mass Storage reads happen only because the **Configurator explicitly requests them**.

The configurator performs an initial device-view refresh when it opens and the MacroPad is connected. The same operation can be requested again with **Refresh Device View**.

This means:

```text
MacroPad connects
    -> daemon opens CDC
    -> daemon uses persistent home cache
    -> no recursive MSC scan

Configurator opens
    -> Configurator requests device refresh
    -> daemon reads MSC on behalf of the Configurator
```

Individual Save/Delete/Firmware operations may access the specific files they need. They are not background recursive scans.

## Synchronization model

The configurator provides three separate synchronization operations.

### Sync MacroPad

```text
MacroPad -> temporary runtime view
```

The physical MacroPad `configs/`, `scripts/` and `app_icons/` directories are mirrored into an ephemeral view under the runtime directory.

This does **not** replace the persistent home cache.

### Save MacroPad settings to HOME

```text
MacroPad -> persistent EezOpen cache
```

Import explicitly replaces the local persistent copy of:

```text
configs/
scripts/
app_icons/
```

Import requires confirmation because persistent local data may be overwritten.

### Save HOME settings to MacroPad

```text
persistent EezOpen cache -> MacroPad
```

Apply mirrors the cached configurable files to the physical device.

The firmware stores the **number of profiles separately from the files**. Therefore EezOpen first infers the highest local profile represented by `configs/`, `scripts/` or `app_icons/` and synchronizes the firmware profile count with the confirmed command:

```text
m<count>
```

It verifies the result with:

```text
l
n=<count>
```

Only then does Apply continue with the cached key files and serial key state. For example, if the home cache contains profile 6 files while the MacroPad currently has 5 profiles, **Apply to MacroPad creates profile 6 automatically**.

## Key file model

A configured key uses the standard per-key triplet:

```text
configs/profile_<profile>_key_<key>.txt
scripts/profile_<profile>_key_<key>.txt
app_icons/profile_<profile>_key_<key>.png
```

For host-side Non-HID actions, the matching script file may be empty because the action is executed by the daemon.

## One icon per key

EezOpen uses a single icon model for all key types, including:

- HID;
- Open URL;
- Launch application;
- Open folder;
- Open file;
- External Script.

There is no multi-key selection state, secondary icon, or runtime PNG replacement.

A selected image is normalized by the configurator to:

```text
64 x 64
8-bit RGBA PNG
PNG color type 6
```

The resulting icon is cached in:

```text
~/.local/share/EezOpen/app_icons/<device-id>/profile_<profile>_key_<key>.png
```

and written to the matching physical `app_icons/` pathname when the operation requires it.

## Non-HID host actions

The daemon supports these action IDs:

```text
ACT 1  Open URL
ACT 2  Launch application/command
ACT 3  Open folder
ACT f  Open file
ACT p  External Script
```

For host-side actions, the MacroPad sends a key event such as:

```text
ebf.k.<profile>.<key>
```

The daemon reads the matching configuration from the persistent cache and runs the configured host action.

### Open URL — ACT 1

Uses `xdg-open`. If the value has no explicit URL scheme, EezOpen prefixes `https://`.

### Launch application — ACT 2

The command is parsed as argv and the executable is resolved through `PATH` when needed.

### Open folder / file — ACT 3 / ACT f

Uses `xdg-open` with the configured path.

## External Script — ACT p

It launches a user-selected executable or script directly. It is not a resident extension subsystem and it has no persistent key-selection state.

The configurator exposes:

```text
Script
Arguments
Icon
```

Normal execution uses:

```text
shell=False
```

Arguments are parsed with argv semantics, so quoting can preserve values that contain spaces.

Example:

```text
Script:    /usr/bin/obs_control.py
Arguments: --scene navegador
```

The executable must exist and be executable, either by path or through `PATH`.

### External Script environment

EezOpen supplies optional context variables:

```text
EEZOPEN_PROFILE
EEZOPEN_KEY
EEZOPEN_DEVICE_ID
EEZOPEN_ALIAS
```

The script does not need to use them.

### Test External Script

The configurator can run the command before saving it.

The test:

- executes directly with `shell=False`;
- waits up to 30 seconds;
- reports the exit code;
- captures stdout/stderr for diagnostics.

Normal key execution is detached from the GUI. A small daemon thread only waits to reap the child process; process lifetime does not control any key visual state.

## HID Mode

HID actions are configured by EezOpen but executed by the MacroPad firmware.

The daemon does not emulate keyboard input.

The editor supports:

```text
Shortcut
Key
Input Text
Wait
```

Confirmed shortcut composition keeps simultaneous keys on the same script line, for example:

```text
CONTROL ALT t
CONTROL E
```

Supported media-script tokens include:

```text
MK_VOLUP
MK_VOLDOWN
MK_MUTE
MK_PREV
MK_NEXT
MK_PP
MK_STOP
```

If an existing HID configuration contains an ACT form the editor does not understand, EezOpen blocks saving instead of silently discarding the unknown action.

## Profiles

Profile count query:

```text
l
```

Expected reply:

```text
n=<count>
```

Profile-count change:

```text
m<count>
```

Profile activation follows the confirmed form:

```text
9<profile>.0
```

The configurator switches the physical MacroPad profile when the user changes the selected editor profile.

## Custom background image

The GTK Configurator can convert a normal image into the MacroPad LVGL background format and install it through USB Mass Storage.

The conversion uses the bundled `create_bg.py` helper and writes its temporary result under:

```text
/tmp/eezopen/
```

The selected image is fitted to **622×345**, quantized to **16 colors**, and encoded as LVGL v8 `INDEXED_4BIT`. The resulting file is then copied to `app_icons/` on the MacroPad.

Background targets:

```text
Dark  -> app_icons/bg_dark.bin
Light -> app_icons/bg_light.bin
```

`bg_dark.bin` is confirmed from analysis of the official Linux configurator. `bg_light.bin` is currently an **experimental filename** and has not yet been confirmed on the physical device.

This is a Configurator-driven Mass Storage operation. The daemon does not scan or write the MacroPad storage in the background; it accesses the volume only after the Configurator sends the explicit background-install IPC request.

After applying a background, rotate the MacroPad wheel to change layout. The firmware reloads the background when the layout changes, so the new image may not become visible until that refresh occurs.

The Configurator also provides **Remove Background**. It explicitly removes both known custom background files (`bg_dark.bin` and `bg_light.bin`) from `app_icons/`. After removal, rotate the MacroPad wheel to change layout and refresh the screen.

Pillow is required for conversion. On Ubuntu/Debian:

```bash
sudo apt install python3-pil
```

## RGB

The current RGB modes are:

```text
Always On
When Pressing The Key
Breath
Flowing
Always Off
```

EezOpen stores the latest RGB state locally and sends it through the daemon.

## Screen Script

The main Configurator window can select a long-running **Screen Script** and turn it on/off with a switch. This is intended for custom display applications such as Linux hardware monitors that continuously draw information on the MacroPad screen.

When enabled:

```text
Configurator -> daemon IPC -> daemon releases CDC -> selected Screen Script starts
```

The daemon intentionally closes its normal MacroPad serial descriptor and suspends redetection while the Screen Script is active. This gives the script temporary ownership of `/dev/ttyACM*`; the built-in PC Monitor is therefore paused automatically during this handoff.

Python files (`.py`) are launched with the current Python interpreter. Other files must be executable. The process is started directly with `shell=False`. EezOpen exports the optional environment variables:

```text
EEZOPEN_TTY
EEZOPEN_DEVICE_ID
EEZOPEN_SCREEN_SCRIPT=1
```

A script may ignore these variables and discover the device itself. The example customised-display monitor uses the confirmed `cus` framing and opens the MacroPad at 115200 baud.

When the switch is turned off, EezOpen terminates the managed process, sends the confirmed customised-display `{"cmd":"stop"}` message as a best-effort cleanup, and resumes normal MacroPad detection. The selected script path is remembered, but **running state is never automatically restored after daemon/login restart**.

The managed script's stdout/stderr is written to:

```text
~/.local/share/EezOpen/screen-script.log
```

## PC Monitor

PC Monitor runs inside the daemon so only one EezOpen process owns the serial descriptor during normal EezOpen runtime. While a Screen Script is enabled, serial ownership is intentionally handed to that script and PC Monitor pauses.

The current implementation sends Linux host telemetry once per second while connected. It uses the confirmed `pcs` framing and the device-specific timestamp correction already validated on the tested firmware/host combination.

Typical data includes CPU, memory, storage, network and GPU information when supported by the host.

## Safe Mode

PID `303a:4005` is treated as the observed Safe Mode runtime where the firmware does not expose USB Mass Storage.

CDC/HID runtime capabilities remain available, including host-side actions and PC Monitor.

Operations requiring physical storage are unavailable, including:

- Refresh Device View;
- Import from MacroPad;
- Apply to MacroPad;
- storage-backed key/icon changes;
- firmware update.

The GUI keeps the controls descriptive with tooltips and reports why a storage-backed action cannot run.

## Configurator tooltips

Interactive controls in the configurator include hover descriptions. This includes synchronization, profile, RGB, firmware, key-editing, HID and External Script controls.

In particular:

- **Refresh Device View** explains that it refreshes only the temporary view;
- **Import from MacroPad** explains the MacroPad-to-home direction;
- **Apply to MacroPad** explains the home-to-MacroPad direction and profile-count synchronization.

## Firmware update

The firmware workflow accepts matching BIN and JSON files, validates them, copies the required update files to the MacroPad storage, and sends the confirmed update trigger using the dedicated firmware-update serial path.

The daemon does not treat the transient updater/recovery personality as a normal runtime device while the firmware transition is in progress.

## Installation

The release directory must contain at least:

```text
eezopen-daemon.py
eezopen-config.py
eezopen.service
install.sh
uninstall.sh
```

Optional assets can include:

```text
eezopen.svg
assets/*.png
```

Run as your normal desktop user:

```bash
./install.sh
```

Do not run the installer as root.

On Ubuntu/Debian, the main GUI/runtime dependencies are typically:

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 xdg-utils udisks2
```

The installer:

- installs the daemon and configurator under `~/.local/bin/`;
- installs the user systemd service;
- installs the desktop entry and optional assets;
- uses the standard `dialout` group for CDC serial access;
- removes obsolete EezOpen-owned udev rules from earlier releases;
- does **not** install a new EezOpen USB authorization/permission rule;
- does **not** run `udevadm trigger`;
- does **not** authorize/deauthorize interfaces or bind/unbind USB drivers.

Start the daemon after the login session has the required `dialout` membership:

```bash
systemctl --user enable --now eezopen.service
```

Open the configurator:

```bash
~/.local/bin/eezopen-config
```

Follow daemon logs:

```bash
journalctl --user -u eezopen.service -f
```

## Uninstallation

Normal uninstall preserves the persistent user configuration:

```bash
./uninstall.sh
```

To remove all EezOpen data as well:

```bash
./uninstall.sh --purge
```

`--purge` removes `~/.local/share/EezOpen`, including any obsolete state directories left by older EezOpen builds.

The uninstaller does not remove the user from `dialout`, because `dialout` is a standard system group that may be used by unrelated serial applications.

## Security model

EezOpen follows these boundaries:

- daemon and GUI run as the logged-in user;
- during normal runtime, the daemon is the only EezOpen process that owns the MacroPad serial port; a user-enabled Screen Script is an explicit temporary handoff;
- the IPC socket is user-private;
- External Script commands are explicitly configured by the user and execute with that user's permissions;
- External Script uses direct argv execution rather than a shell;
- Screen Script execution is explicitly user-enabled, runs with the user's permissions, and temporarily owns the MacroPad serial port;
- the installer does not manipulate USB interface authorization or driver binding;
- Mass Storage recursive reads are driven by explicit Configurator workflows rather than the daemon connection loop.

Because External Script can execute arbitrary user-selected programs, users should review commands/scripts before assigning them to a key.

## Protocol notes

Normal serial command framing uses:

```text
"ebf" + uint8(payload_length) + ASCII payload
```

Host-side key events received from the MacroPad use:

```text
ebf.k.<profile>.<key>
```

EezOpen intentionally avoids inventing protocol commands. Device behavior should be backed by physical tests, captures, official documentation, or analysis of the official configurator.

## Design boundaries

Current architecture intentionally keeps these rules simple:

- no resident extension subsystem;
- no multi-key selection state;
- no secondary icon state;
- no dynamic runtime PNG swapping;
- one icon per key;
- External Script is a direct host-side key action;
- Screen Script is a separately managed long-running display process with explicit serial handoff;
- the daemon does not recursively scan Mass Storage on device connection;
- Configurator-driven synchronization owns recursive Mass Storage workflows;
- Apply to MacroPad synchronizes the firmware profile count from local files before applying key state.