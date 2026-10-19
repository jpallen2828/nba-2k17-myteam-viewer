# NBA 2K17 MyTEAM v1.3.0

Updated October 19, 2026.

- Adds a dedicated Blacktop mode beside Draft a Team, with 1v1 through 5v5 formats and Baller, Default, Budget, and Super Budget settings.
- Applies the Guard/Wing/Big eligibility rules and a bonus round to each Blacktop draft. The fourth 5v5 slot rolls Wing or Big once and retains that roll when resumed.
- Injects every displayed Blacktop card, including the bonus. Incoming players and destination slots are paired by descending overall; ties retain their original order. Destination controls show the automatic assignments.
- Detects the supported live Play Now Blacktop roster directly. Traditional team-mode injection retains its existing slot and position workflow.
- Automatically saves traditional and Blacktop drafts locally, including unfinished choices and settings. Older Blacktop saves retain every selected player.
- Adds optional draft music with Play/Pause, Restart, and a volume slider with a visible white track.
- Improves image-cache limits, dialog and worker cleanup, database connection handling, and repeated-screen resource use.
- Updates Game Connection and the reviewed hook compatibility profile, while retaining executable, dependency, configuration, and conflicting-file checks.
- Includes 2,182 available cards, including Darrell Griffith 1984, saved custom cards, reusable injection templates, and original PNG artwork. Artwork is not recompressed.
- Updates source code, regression checks, screenshots, matching build resources, and SHA-256 checksums.

Download **NBA.2K17.MyTEAM.zip**, extract the entire archive, and run **2K17 MyTEAM.exe**. Keep the included `.project` folder beside the EXE. The build-resources download is optional and only needed to build from source.

For an existing installation, follow [UPDATING.md](UPDATING.md). Extract into a new folder and preserve your previous installation until your personal cards and lineups have been checked.

Compatibility remains limited to verified NBA 2K17 executable/roster layouts. External modded face and portrait assets are not included. Memory readback verifies stored fields; confirm the resulting appearance and presentation in the game.
