# Building ATEM-EDL-TOOL for macOS

## Before building

Build on a Mac with Python 3.12 and Xcode command line tools. The bundled PyQt6 and Python libraries determine the app's processor architecture: a build made here on Apple Silicon is for Apple Silicon Macs. To serve Intel Macs, build separately on Intel and label that archive accordingly.

The app entry point is `atemlogger7.py`. The PyInstaller configuration is `atemlogger.spec`; dependencies are pinned in `requirements-build.txt`. Keep the bundle identifier `com.teliotortay.atemlogger` stable so macOS can recognize the app and its Local Network permission across updates.

## Build after every code change

From the repository root, run:

```sh
./build-mac.sh
```

If Python 3.12 is not on `PATH`, set `PYTHON_BIN` to its executable path. This produces `dist/ATEM-EDL-TOOL.app` with an ad hoc signature for local testing. It is not a shareable release build.

For a build to send to another Mac, first identify an installed **Developer ID Application** certificate with `security find-identity -v -p codesigning`, then run:

```sh
ATEMLOGGER_CODESIGN_IDENTITY='Developer ID Application: YOUR NAME (TEAMID)' ./build-mac.sh
```

The build script creates and updates `.build-venv`, rebuilds with PyInstaller, verifies the app's signature, and creates `dist/ATEM-EDL-TOOL-Apple-Silicon.zip` or `dist/ATEM-EDL-TOOL-Intel.zip` for transfer. The `ATEMLOGGER_CODESIGN_IDENTITY` variable is read by `atemlogger.spec`; keep the certificate name and any signing credentials out of source control. Run the signed build from a normal Terminal session with access to the signing keychain.

## Check the result

1. Confirm `codesign --verify --deep --strict 'dist/ATEM-EDL-TOOL.app'` succeeds.
2. Confirm `codesign -dv --verbose=2 'dist/ATEM-EDL-TOOL.app' 2>&1` shows the intended Developer ID authority and Team ID. An ad hoc signature is not a release signature.
3. Extract the ZIP on a test Mac and open the extracted app. Check that the window opens, input settings are present, and the current code change works. A hardware connection is not needed for the launch check.
4. If EDL export changed, export a small log and reimport it. Verify the camera reel number, `_camera-number` clip-name suffix, timecodes, and original source name. Do not rely on an old file in `dist/` as evidence of the new code.

Share the ZIP rather than copying the bare `.app` bundle, which can lose required files or symlinks during transfer. After extraction, move the app to Applications before opening it.

## Gatekeeper and notarization

Developer ID signing does **not** mean the build is notarized. Until the app has been submitted to Apple's notary service and its ticket stapled, macOS may require a one-time **System Settings → Privacy & Security → Open Anyway** approval. If a user cancels that approval, have them try opening the app again, then complete the approval in Privacy & Security. Do not remove quarantine attributes or disable Gatekeeper as a workaround.

For release without that manual approval, notarize the Developer ID signed app with an authorized Apple notary profile, staple the ticket, and verify it with `xcrun stapler validate`. Never claim a build is notarized until that check succeeds. Package the stapled app into a fresh ZIP afterward.
