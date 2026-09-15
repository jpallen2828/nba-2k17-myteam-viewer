# Updating without losing your cards

For the first installation, extract the entire ZIP into a writable folder and run the EXE. Keep `.project` beside it.

For an update:

1. Save your work and close both the old and new MyTEAM applications.
2. Extract the new release to a different folder. Do not overwrite the old folder.
3. Run `Import previous user data.cmd` inside the new folder and select the old folder containing `2K17 MyTEAM.exe`.
4. The helper copies custom imports, saved teams, disabled/deleted-card choices, and custom team/year overrides. It retains the new release's bundled database and cards.
5. Open the new app and check your cards and lineups before removing the old installation.

The helper reads the old folder without changing it. If the same imported file exists with different contents, it stops and asks you to use a freshly extracted destination rather than overwrite the conflict.

Projects saved in your Documents folder or another location stay there. Studio save-folder preferences are stored in your Windows user settings. Manually copying an old database over the new one would discard updated bundled data; use the helper instead.
