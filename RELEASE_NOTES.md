# NBA 2K17 MyTEAM v1.5.0

Updated November 8, 2026.

- Adds editable wingspan directly below height in Card Studio. Choose Original to keep the captured length, or Custom to use a 0–100 slider with live centimeters and feet/inches.
- Saves custom wingspan settings in editable projects and exported cards, and applies length and scale during injection with ownership checks, readback, rollback and Undo.
- Shows both Original and Custom together when hovering over the mode selector, with no scrolling.
- Adds wingspan to the archive Information tab. This displays only centimeters and feet/inches, without the mode label or slider number.
- Includes all 2,200 saved cards and their original PNG artwork. Existing captured lengths remain unchanged unless a custom override is selected.
- Retains the player-ID, native sculpt and full-name presentation fixes from v1.4.0, along with all team and draft modes.

The custom formula matches the edited Curry and Bol captures within 0.001 cm using standard body proportions. It is not a replacement for every native skeletal adjustment. The new slider's in-game visual result still requires testing; memory readback alone does not establish appearance.

Download **NBA.2K17.MyTEAM.zip**, extract the entire archive, and run **2K17 MyTEAM.exe**. Keep the included `.project` folder beside the EXE. **NBA.2K17.Build.Resources.zip** is optional and only needed to build from source.

Existing users: follow [UPDATING.md](https://github.com/jpallen2828/nba-2k17-myteam-viewer/blob/main/UPDATING.md) to preserve personal cards and lineups. External modded game assets are not included.

Validation covers wingspan calculations, saved-card export, zero and repeated settings, linked-storage protection, readback, rollback and Undo. The packaged interface check passed with all 2,200 cards. Release packaging verifies extraction, PNG hashes, card values and reusable templates.
