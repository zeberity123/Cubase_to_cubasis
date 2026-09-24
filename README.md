# Cubase to Cubasis — Song Exporter 0.3

A standalone Windows app that reads a saved Cubase `.cpr`, shows its song folders with checkboxes, and exports each checked song as a separate `.dawproject` containing its audio. It reads the folder tree from the selected project; `song-folders.txt` is not required.

## Use the Windows app

1. Download the Windows package from [Releases](https://github.com/zeberity123/Cubase_to_cubasis/releases), extract it, and run **`CubaseSongExporter.exe`**. No Python installation is needed.
2. Click **Open .cpr** and choose your saved Cubase project.
3. Check individual song folders. Parent folders stay visible for navigation. A checked song includes audio tracks in its nested folders; selecting both a song and its descendant is prevented.
4. Check the **BPM** for each selected song. Values are suggested from folder names, including decimals; select a row to correct its BPM in the right panel.
5. Choose an **Output folder**, then click **Export selected songs**.
6. Transfer the resulting `.dawproject` files to iOS and open/import them in Cubasis. The WAV files are already inside each project.

Each output is named after its song folder, for example `Song170.dawproject`. Existing files are retained and a numbered filename is used for another export. A per-song `.report.json` records clip timing, audio hashes, source paths and warnings. An `export-summary-*.json` lists completed and failed songs. Those reports can stay on Windows.

Opening the project only reads the CPR, not the whole audio library. Export packages only the audio referenced by the selected songs. A referenced WAV is usually included in full, so an output can include unused parts of that file. The source CPR and WAVs are never edited. All processing stays on this computer.

**Original clip positions are retained.** Musical positions are evaluated at the BPM chosen for that song; the first clip is not moved to zero. Gaps, source offsets and separate clips are preserved for the supported event layout. Nested track names are flattened as `subfolder / track` in the output. Outputs use a constant BPM and 4/4.

If media was moved, choose **Extra audio folder**. The app checks saved paths, the CPR's `Audio` and `Edits` folders, and the optional folder. It validates file length, sample rate, channel count and bit depth before packaging. It does not search every disk for similarly named audio.

## Scope of this first release

This is an experimental, sample-driven reader for the observed Cubase 12 CPR structure, not a complete implementation of Cubase's proprietary format. It was developed against a project saved by Cubase 12.0.30, targeting Cubasis 3.8.6 on iOS.

- Supports ordinary mono/stereo WAV clips, PCM 16/24/32 and 32-bit float WAVs, cuts, gaps, source trims and static clip gain. Simple processed source segments are reconstructed; integer bit depths are promoted without loss when needed. Clip gain is rendered into separate float WAVs.
- Omits MIDI, instruments, plug-ins, mixer settings, pan, track/folder mute and solo, automation, tempo changes and time-signature changes. Audio tracks use unity volume and center pan.
- Known unsupported fades/crossfades, clip envelopes, nested audio parts, pitch edits, invalid source bounds and unknown event layouts stop that song with an error. Other selected songs continue. Time stretching/AudioWarp is not implemented; detection of every possible unsupported edit is not guaranteed.
- Overlapping clips retain their geometry, but Cubase's event playback priorities are not mapped. Compare those regions in Cubasis.
- Event flag `0x0002` is interpreted as a disabled clip. This interpretation has not yet been independently checked against a muted Cubase XML sample or on iOS.
- This app does not recreate the Cubase mix or export a native Cubasis `.cbp` file.

The app displays unsupported-song errors rather than claiming an incomplete song was exported. If a song fails, its error identifies the track/feature where possible. A rendered source from Cubase or further decoder support is needed for those cases.

## Validation and import history

The private regression project contains **137 folders and 1,435 audio tracks**. All declared folder child counts match the decoded hierarchy. Its audio library does not have to be copied to list songs. Private projects, audio, folder inventories and conversion reports are not included in this repository.

A sample converted through the XML archive path was confirmed to import and play correctly in Cubasis 3.8.6. This app uses that same DAWproject structure: per-track Lanes/Clips, arrangement coordinates in beats, source coordinates in seconds, ASCII packaged media names and ordinary ZIP headers.

Native CPR clip timing and source trims were compared with a Cubase XML export. The app exports the edits in the CPR currently selected, so save edits in Cubase before loading it.

The new native CPR export path still needs an end-to-end playback check on your iPad. Desktop tests cannot establish that every converted song will play identically in Cubasis.

Release checks: 21 automated tests passed with the private fixtures present. The packaged executable passed a hidden GUI/folder-selection check with Python absent from PATH, then exported a song with six tracks, 24 clips and six audio files. The resulting project passed both XML schemas, ZIP integrity, native clip position/length/offset comparisons and packaged audio hash checks.

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

The build writes `dist/CubaseSongExporter.exe`, the README and runtime notices. The tested build uses Python 3.10.7, NumPy 1.23.5, SoundFile 0.12.1 and PyInstaller 6.11.0. The executable contains Python, Tcl/Tk and the audio runtime.

Tests cover clip cuts/positions, source trims, Unicode names/paths, media references, PCM reconstruction, float audio, cancellation, overwrite protection, unsupported edits and the private project's hierarchy. Private sample tests skip when their source files are unavailable. `CUBASIS_PRIVATE_CPR` optionally points to the original private regression CPR; those tests are fixture-specific, not checks for an arbitrary project. XML regression fixtures belong in the ignored `sample/` directory.

Third-party runtime license notices accompany the Windows release in `licenses/`. The license in `reference/` applies to the upstream DAWproject reference files.

`reference/` contains DAWproject schemas, Clip documentation and the upstream MIT license from https://github.com/bitwig/dawproject. Official Cubasis import instructions: https://www.steinberg.help/r/cubasis/3.8/en/cubasis/topics/dawproject_files_importing_t.html
