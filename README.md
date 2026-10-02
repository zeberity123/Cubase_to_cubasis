# Cubase to Cubasis — Song Exporter 0.3.4

A standalone Windows app that reads a saved Cubase `.cpr`, shows its song folders with checkboxes, and exports each checked song as a separate `.dawproject` containing its audio. It reads the folder tree from the selected project; `song-folders.txt` is not required.

Version 0.3.4 adds an opt-in **Allow silent source tails** setting and native decoding and audio rendering for observed linear clip volume envelopes and fade curves, accepts empty fade objects, flattens supported audio parts, and preserves negative arrangement positions. It retains checkboxes for individual audio tracks, failed-track highlighting, and a **Clear / select failed songs** menu. Unchecked tracks are excluded before decoding and their omissions are recorded in the output reports. All tracks start checked. Project/output settings and the export log can be expanded when needed to leave more space for the lists.

## Use the Windows app

1. Download the Windows package from [Releases](https://github.com/zeberity123/Cubase_to_cubasis/releases), extract it, and run **`CubaseSongExporter.exe`**. No Python installation is needed.
2. Click **Open .cpr** and choose your saved Cubase project. The settings area collapses after loading; click **Project / output folders** to reopen it.
3. Check individual song folders. Parent folders stay visible for navigation. A checked song includes audio tracks in its nested folders; selecting both a song and its descendant is prevented.
4. Check the **BPM** for each selected song. Values are suggested from folder names, including decimals; select a row to correct its BPM in the right panel.
5. Uncheck any audio tracks you want to exclude in the right panel. Open **Project / output folders** to choose an **Output folder**, then click **Export selected songs**.
6. Transfer the resulting `.dawproject` files to iOS and open/import them in Cubasis. The WAV files are already inside each project.

Each output is named after its song folder, for example `Song170.dawproject`. Existing files are retained and a numbered filename is used for another export. A per-song `.report.json` records clip timing, audio hashes, source paths and warnings. An `export-summary-*.json` lists completed and failed songs. Those reports can stay on Windows.

Opening the project only reads the CPR, not the whole audio library. Export packages only the audio referenced by the selected songs. A referenced WAV is usually included in full, so an output can include unused parts of that file. The source CPR and WAVs are never edited. All processing stays on this computer.

**Original clip positions are retained.** Musical positions are evaluated at the BPM chosen for that song; the first clip is not moved to zero. Gaps, source offsets and separate clips are preserved for the supported event layout. Nested track names are flattened as `subfolder / track` in the output. Outputs use a constant BPM and 4/4.

If media was moved, choose **Extra audio folder**. The app checks saved paths, the CPR's `Audio` and `Edits` folders, and the optional folder. It validates file length, sample rate, channel count and bit depth before packaging. It does not search every disk for similarly named audio.

## Retry failed songs

1. Use **Clear / select failed songs** beside **Open output folder**, then choose **Select failed songs**. This clears the existing song selection and checks unresolved failures from this session.
2. For failures from an earlier run, choose **Load failed songs from summary...** and open that run's `export-summary-*.json`. The current project must match the summary. Older summary formats are supported; ambiguous duplicate song names are rejected.
3. Select a failed song. The track responsible for its latest failure is marked **[failed]** in red when it can be identified. Uncheck that track, or any other unnecessary tracks, then export again.

Track choices persist while switching folders and across retries. If the same track appears under a parent and child folder, its checkbox applies to both. Loading a project resets track choices and failure history. A successful retry removes that song from the unresolved failure list. All remaining checked tracks must be supported; another unsupported track may be reported on the next attempt.

The retry export deliberately omits unchecked tracks. Its `.report.json` and the batch summary record those exclusions. Use **Show log** to inspect messages. **Clear song selection** clears song checkboxes without changing track choices.

Linear clip volume envelopes and supported fade curves are rendered into trimmed 32-bit float WAVs. Empty fade objects are accepted without changing the audio. Supported audio parts are flattened while retaining their positions, source offsets, bounds and disabled flags. Negative arrangement positions are preserved; verify playback before zero in Cubasis.

**Allow silent source tails** in the right panel fills an event's portion beyond its source WAV with silence while preserving its position, length and existing audio speed. It starts unchecked. This is an intentional alternative to Cubase playback: stretched audio that originally continued playing will become silent there. It can affect musical stems as well as click tracks. Each affected event is recorded in the report. Time stretching/AudioWarp remains unsupported; leave this setting off and render affected tracks in Cubase if you need the original stretched playback. Unknown processing attributes, unsupported fade shapes and overlapping event fades still stop that song.

## Scope of this first release

This is an experimental, sample-driven reader for the observed Cubase 12 CPR structure, not a complete implementation of Cubase's proprietary format. It was developed against a project saved by Cubase 12.0.30, targeting Cubasis 3.8.6 on iOS.

- Supports ordinary mono/stereo WAV clips, PCM 16/24/32 and 32-bit float WAVs, cuts, gaps, source trims and static clip gain. Simple processed source segments are reconstructed; integer bit depths are promoted without loss when needed. Clip gain is rendered into separate float WAVs.
- Omits MIDI, instruments, plug-ins, mixer settings, pan, track/folder mute and solo, automation, tempo changes and time-signature changes. Audio tracks use unity volume and center pan.
- Unsupported fade shapes/crossfade behavior, pitch edits, invalid source bounds and unknown event layouts stop that song with an error. Other selected songs continue. Time stretching/AudioWarp is not implemented; detection of every possible unsupported edit is not guaranteed.
- Overlapping clips retain their geometry, but Cubase's event playback priorities are not mapped. Compare those regions in Cubasis.
- Event flag `0x0002` is interpreted as a disabled clip. This interpretation has not yet been independently checked against a muted Cubase XML sample or on iOS.
- This app does not recreate the Cubase mix or export a native Cubasis `.cbp` file.

The app displays unsupported-song errors rather than claiming an incomplete song was exported. If a song fails, its error identifies the track/feature where possible. A rendered source from Cubase or further decoder support is needed for those cases.

## Validation and import history

The private regression project contains **137 folders and 1,435 audio tracks**. All declared folder child counts match the decoded hierarchy. Its audio library does not have to be copied to list songs. Private projects, audio, folder inventories and conversion reports are not included in this repository.

A sample converted through the XML archive path was confirmed to import and play correctly in Cubasis 3.8.6. This app uses that same DAWproject structure: per-track Lanes/Clips, arrangement coordinates in beats, source coordinates in seconds, ASCII packaged media names and ordinary ZIP headers.

Native CPR clip timing and source trims were compared with a Cubase XML export. The app exports the edits in the CPR currently selected, so save edits in Cubase before loading it.

The new native CPR export path still needs an end-to-end playback check on your iPad. Desktop tests cannot establish that every converted song will play identically in Cubasis.

Version 0.3.4 checks: 30 automated tests passed; two private-fixture test groups were skipped. Against the supplied project, 38 previously rejected audio tracks now decode, with no regressions among 1,441 unique audio tracks. Seven of the 14 reported songs pass full-song preflight with strict source bounds; all 14 pass with silent source tails enabled. One folder requires a user-entered BPM; the preflight used a placeholder for structural checks. Representative one-track exports from each passing song passed ZIP integrity, both XML schemas, clip timing and rendered waveform sample checks. The XML exports independently confirm decoded event timing and the observed volume envelope points. These checks do not establish identical Cubase playback for all edits.

## Python tools and development

The previous XML archive converter remains available:

```powershell
python cubase_to_cubasis.py sample/sample.xml output/another-project.dawproject --tempo 165
```

Read-only folder inventory:

```powershell
python cpr_inventory.py "C:\Projects\Example\Project.cpr"
```

Run the GUI from source using Python 3.10 (the pinned NumPy build targets this version):

```powershell
python -m pip install -r requirements.txt
python song_exporter_app.py
python -m unittest discover -s tests -v
```

To build the executable on Windows:

```powershell
python -m pip install -r requirements-dev.txt
.\build_app.ps1
```

The build writes `dist/CubaseSongExporter.exe`, the README and runtime notices. To build separately while an existing executable is running, use `.\build_app.ps1 -OutputDirectory dist\v0.3.4`. The tested build uses Python 3.10.7, NumPy 1.23.5, SoundFile 0.12.1 and PyInstaller 6.11.0. The executable contains Python, Tcl/Tk and the audio runtime.

Tests cover clip cuts/positions, source trims, Unicode names/paths, media references, PCM reconstruction, float audio, cancellation, overwrite protection, unsupported edits and the private project's hierarchy. Private sample tests skip when their source files are unavailable. `CUBASIS_PRIVATE_CPR` optionally points to the original private regression CPR; those tests are fixture-specific, not checks for an arbitrary project. XML regression fixtures belong in the ignored `sample/` directory.

Third-party runtime license notices accompany the Windows release in `licenses/`. The license in `reference/` applies to the upstream DAWproject reference files.

`reference/` contains DAWproject schemas, Clip documentation and the upstream MIT license from https://github.com/bitwig/dawproject. Official Cubasis import instructions: https://www.steinberg.help/r/cubasis/3.8/en/cubasis/topics/dawproject_files_importing_t.html
