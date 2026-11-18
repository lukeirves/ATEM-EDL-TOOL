# Agent build instructions

After changing the app, follow [BUILDING.md](BUILDING.md) before handing it over.

- Build from this repository with `./build-mac.sh`; do not share an older app or ZIP from `dist/`.
- For another Mac, use the installed Developer ID Application identity through `ATEMLOGGER_CODESIGN_IDENTITY`. The script verifies the signature and creates a ZIP when that variable is set.
- Confirm the new app launches, and check an EDL export with the changed behavior. Do not require an ATEM or HyperDeck for the launch check.
- Share the ZIP, not the bare `.app`. State the supported processor architecture and whether the build is notarized. Developer ID signing alone does not notarize it.
- Do not add passwords, Apple account credentials, or private signing keys to this repository.
