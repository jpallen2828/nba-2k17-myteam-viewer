# Card Studio

Open **Card Studio** in the main MyTEAM window. A separate Studio download is not required.

1. Select a card tier, team logo, promotion, player image, name, jersey number, and year.
2. Import an existing card's player data to use its saved player template as the starting point.
3. Open the player-data editor to adjust vitals, attributes, tendencies, signatures, shoes/gear, accessories, badges, and hot zones.
4. The overall calculation updates as attributes change and shows two decimal places in the editor. Card artwork uses the displayed integer overall.
5. Save the editable project, or export **Card + Data (.2k17custom)** and import it into the archive.

A PNG alone is artwork; it does not contain the player data required for injection. Keep the editable project or `.2k17custom` package.

## Wingspan

Wingspan appears directly below height in the player-data editor. Hover over its mode selector to see **Original** and **Custom** together.

- **Original** preserves the captured source length. An untouched in-game slider at 50 can have a different length from a manually selected 50.
- **Custom** enables the 0–100 slider and numeric control. The corresponding length updates in centimeters and feet/inches as you change the slider or height. The value is saved with the card and used for injection.
- **Restore original** removes the custom override and returns to the captured length.

Custom length uses `height_cm × 1.0544021 × (0.9 + 0.002 × slider)`, calibrated against edited Stephen Curry and Manute Bol captures with standard body proportions. It does not reproduce every native skeletal adjustment for other body shapes. Custom length and scale writes retain ownership checks, readback, rollback and Undo.

The archive's **Information** tab shows only the length in centimeters and feet/inches, without a mode label or slider number. If no source length is available, it displays **Unavailable**.

The selected team logo supplies the card's team metadata. Use **Manage custom cards** in the archive to edit custom-card teams and years or disable/re-enable cards. Permanent removal is separate from disabling.

Use Studio's default save-folder setting to choose where new local projects are saved. The background-removal model runs locally; it does not upload the player image.

External modded faces and game portraits are separate from MyTEAM card artwork. Importing a custom card does not install those game assets.
