# ATEM-EDL-TOOL

**ATEM-EDL-TOOL** is a powerful and intuitive tool designed for video production professionals. It allows you to track ATEM video inputs and capture HyperDeck timecodes in real-time. The software automatically generates logs and EDL (Edit Decision List) files, providing detailed information for every cut made during production.

## Features

- **ATEM Video Input Tracking**: Connects directly to ATEM devices over a local network to track program changes and generate detailed logs.
- **Input Name Overrides**: Shows names configured on the ATEM. Check **Override** beside an input and enter a custom name to use it in the live display and EDL. Uncheck it to use the ATEM name again. Overrides are remembered between launches.
- **HyperDeck Timecode Capture**: Captures live playback timecodes from HyperDeck devices.
- **EDL and CSV Export**: Saves source names, start and end timecodes, and CSV durations when you click Save.
- **CuePilot Camera Mapping**: Assign a CuePilot camera number (1–48) to each ATEM input. The EDL uses that number as its reel ID and appends `_number` to the clip name for CuePilot. The exported clip name is limited to eight characters, with the complete source name retained in a separate EDL comment and the CSV. Camera numbers are saved between launches and can also be corrected in the log.
- **Editable Log and EDL Import**: After stopping, edit a row's source, start, or end timecode directly. Import a saved EDL to make corrections and export it again. The duration column updates automatically.
- **Recovery Sessions**: Save the raw log as an `.atemlog` session even when invalid timecodes prevent EDL export. Open the session later with its frame rate, export settings, raw cuts, and error highlights restored.
- **Incoming FPS Warning**: Flags timecodes whose frame number is invalid for the selected FPS, including drop-frame separator mismatches. Invalid cells are highlighted for correction.
- **Detected HyperDeck FPS**: Shows the frame rate reported by the HyperDeck video format beside the live timecode and warns if it differs from the selected export rate.
- **Stable Live Display**: Keeps the monospace timecode fixed in place while it rolls, and scrolls the cut log to each newly recorded row.
- **Export Timecode Offset**: Optionally shift exported timecodes by a signed number of hours without changing the recorded log.
- **Connection and Session Controls**: Remembers both IP addresses, shows ATEM connection status, and locks editing and export while armed or recording.
- **Frame Rate Selection**: Supports 23.976, 24, 25, 29.97, 30, 47.952, 48, 50, 59.94, and 60 fps, with drop-frame and non-drop-frame choices at 29.97 and 59.94.
- **Frame Compensation Management**: Adjusts timecodes to compensate for synchronization differences, ensuring precise recordings even with delays.

## Installation

### Requirements
- **Windows, macOS, or Linux** (any modern version)
- **ATEM Device** (for video input tracking)
- **HyperDeck Device** (for timecode capture)

### Librairies
- **PyQt6**
- **PyATEMMax**

## Build the macOS app

Follow [BUILDING.md](BUILDING.md) after each code change. Install Python 3.12, then run `./build-mac.sh` on a Mac. If Python 3.12 is not on your `PATH`, run `PYTHON_BIN=/path/to/python3.12 ./build-mac.sh`. The script installs the build dependencies in `.build-venv` and creates `dist/ATEM-EDL-TOOL.app`. The app built on an Apple Silicon Mac is for Apple Silicon Macs; build on Intel for an Intel version. This local build is not notarized for distribution.

For a Developer ID signed build, set `ATEMLOGGER_CODESIGN_IDENTITY` to the installed **Developer ID Application** certificate before running the build script. It packages a ZIP to share instead of the bare `.app` bundle. Developer ID signing does not notarize the app; notarization requires a separate Apple notary submission.

On another Mac, extract the ZIP, move **ATEM-EDL-TOOL.app** to **Applications**, and open it. If macOS blocks the first launch, try opening the app once, then go to **System Settings → Privacy & Security → Open Anyway**. If the approval was cancelled, repeat this process. If the app still bounces and closes, launch `/Applications/ATEM-EDL-TOOL.app/Contents/MacOS/ATEM-EDL-TOOL` in Terminal and capture the error output for diagnosis.

After pressing **Stop**, click **Save EDL + CSV now** to export the completed log. The app shows the saved path or a save error below the button. Select the correct frame rate before logging; durations, frame compensation, and exports use that setting. The initial default is 24 fps. The app remembers the frame rate, IP addresses, frame compensation, auto-start choice, export offset, custom input names, last save location, and window size.

The current camera appears in the log as soon as it starts, and its end timecode updates while logging. After stopping, you can click a **Source**, **Start**, or **End** cell to correct it. A ripple edit moves only the adjacent cut point: changing a Start can move the previous clip’s End, and changing an End can move the next clip’s Start. The neighboring clip’s other timecode stays fixed. If that would make one or more clips zero length or invalid, the app asks before removing them. You can also keep the edit on one clip or cancel it. Orange cells contain timecodes that cannot be exported at the selected FPS; hover over a cell for the reason. Choose **Import EDL…** to reopen an EDL. New EDLs record the selected FPS in a comment so it can be restored on import. Older EDLs may not specify the exact FPS, so check the selector before saving. Imported EDL timecodes already include any previous export adjustment; frame compensation and export offset are turned off on import to avoid applying them again.

For CuePilot, check the **CuePilot #** column in Available Inputs before logging. It defaults to the ATEM input number, but you can set a different number for each input; the mapping is remembered. The log's **Camera #** column can be edited after stopping. Changing one camera number updates every row with the same source name. An unmapped camera number is highlighted and blocks EDL export until corrected. Old EDLs with `AX` reel IDs can be imported and assigned camera numbers before exporting again.

The **FPS** readout beside the timecode comes from the HyperDeck's reported video format. It shows a dash until the deck reports a recognized format. The readout is for reference; change the **Timecode frame rate** selector if you want exports to use that rate. For 29.97 and 59.94, select drop-frame or non-drop-frame to match the timecode convention.

If EDL export reports an invalid timecode, use **Save Session…** after stopping. This writes a `.atemlog` recovery file without changing or discarding any captured timecodes. Later, choose **Open Session…**; invalid cells are highlighted again and the error summary lists their rows. Correct the cells or frame rate, then choose **Save EDL + CSV now…**. Session saving is manual and available even if the log cannot be exported as an EDL.

For automatic logging, check **Auto start when HyperDeck timecode runs**, then click **Arm**. The button reads **Disarm** while waiting and **Stop** once timecode advances and logging begins. Click **Disarm** to cancel the wait, or **Stop** to finish a run. It returns to **Arm** for the next run. Click **Save EDL + CSV now** to export the completed log.

Arm requires two valid forward timecode advances close together before logging starts. A malformed HyperDeck reading is shown as an ignored timecode warning and never used as a clip start or end. The most recent ignored reading remains visible and is included in a recovery session. A stopped timecode by itself keeps the app armed.

Click **Connect** before starting or arming. On first launch, macOS may ask for Local Network access so the app can reach the ATEM and HyperDeck. Allow access; the connection light stays yellow while the app retries with fresh sockets and waits for ATEM input names, then turns green. If permission was denied, enable ATEM-EDL-TOOL in **System Settings → Privacy & Security → Local Network** and click Connect again. When replacing the app, remove older renamed copies so you know which build is running.
