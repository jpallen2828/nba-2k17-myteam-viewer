# NBA 2K17 MyTEAM v1.4.0

Updated November 7, 2026.

- Includes 2,200 cards and their original PNG artwork, with the latest saved custom cards and reusable templates.
- Fixes Rasheed Wallace injection rejecting Face ID 389 when an editor or imported card stores the ID as text. Decimal player IDs now normalize before injection; native bounds and invalid-value checks remain enforced.
- Adds Shai Gilgeous-Alexander's full hyphenated name to his four cards and artwork. Game Connection includes a live companion for full menu and jersey names while MyTEAM remains open. Menu and jersey presentation were confirmed after save/reload on the supported game build.
- Corrects native first-name field boundaries and handles the separate localized surname selector explicitly, with preview, readback, rollback and Undo coverage.
- Includes the latest player appearance corrections and reusable native sculpt transfer support, plus linked-storage ownership checks.
- Retains Blacktop drafts, all draft budgets, automatic draft saves, card editing, draft audio and existing injection modes.

Download **NBA.2K17.MyTEAM.zip**, extract the entire archive, and run **2K17 MyTEAM.exe**. Keep the included `.project` folder beside the EXE. The build-resources ZIP is optional and only needed to build from source.

Existing users: follow [UPDATING.md](UPDATING.md) to preserve personal cards and lineups. The separate live name companion needs MyTEAM open while playing. External modded face/portrait assets are not included.

Validation includes 42 targeted numeric-ID/injection tests, native name-companion lifecycle checks, a packaged startup check and complete archive extraction checks. The requested 13-card Bulls injection matched all 4,069 mapped fields on immediate readback. Memory readback and visual/game-save confirmation are reported separately.
