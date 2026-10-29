# Gifski WebUI

A local browser interface for Gifski and FFmpeg. Choose a recipe, estimate file size, or let Smart search for a high-quality GIF within your size limit. Advanced exposes every Gifski CLI encoding option.

## Start

### Windows

1. Download the repository ZIP and extract it, or clone the repository.
2. Install **Python 3.10 or newer** from [python.org](https://www.python.org/downloads/) if needed.
3. Double-click **Start-Windows.cmd**. Keep its terminal open.
4. Follow the dependency setup wizard. Automatic detection checks PATH before the app’s `bin/` folder. **Settings** shows each active executable, version, and detection source; an explicit path overrides automatic detection.
5. On Windows x64, choose **Set up missing tools** to download missing dependencies into the app’s `bin/` folder. Existing PATH tools are kept. Downloads require an install action; no administrator access or PATH changes are needed. FFprobe is included with FFmpeg.

The browser opens at `http://127.0.0.1:8765`. No pip packages, Node.js, account, or web build is required. Installing dependencies needs an internet connection; ordinary encoding stays local.

### macOS / Linux

Install Python 3.10+, Gifski, FFmpeg, and FFprobe with your preferred package manager, then run:

```sh
sh start.sh
```

Automatic portable installation is Windows x64 only. Setup gives manual guidance for other systems. To use another port, run `python3 server.py --port 8766` (Windows: `py -3 server.py --port 8766`).

## Workflow

1. **Choose a source.** Drop a video or PNG sequence, choose files, or enter a local path to avoid making an upload copy.
2. **Choose a mode.** **Default** contains built-in and saved starting recipes, with an Edit in Advanced action for tuning. **Smart** searches within a chosen file-size limit. **Advanced** gives full manual control of Gifski and FFmpeg preprocessing. These are the only three top-level modes.
3. **Set the clip.** Keep the full source or adjust trimming and playback. Source-match buttons sit beside dimensions, FPS, and trim end where applicable.
4. **Encode.** Use **Encode GIF** for a manual recipe or **Find best fit** for Smart. The chosen output, measured size, and download appear in the activity column; other tested outputs stay in a collapsed list.

Controls stay on the left. Process status, logs, results, and history share the right column; the layout stacks on narrow screens. The fixed bottom toolbar holds the output summary, size estimate, and encode actions. Estimate details expand above it without scrolling the page. Settings opens a dialog. History uses small static thumbnails rather than loading every full GIF.

Default includes Balanced, Compact, Fidelity, UI & text, Motion, and Transparent starting recipes. Saved recipes record their mode and settings and can be imported/exported as JSON. Saved Smart recipes restore their budget and pins in Smart; fixed recipes load in Default and can be edited in Advanced. Older fixed-mode recipes migrate to Default, and legacy Discord recipes become Smart at 20 MB. Media paths are not included in exported recipes.

### Smart size fitting

Smart uses a customizable **decimal MB** limit: `20 MB` means exactly `20,000,000 bytes`. Short samples guide the search; a fit is confirmed only when a complete GIF is measured at or below the limit.

By default, Smart can vary resolution, frame rate, and encoder quality. **Pin resolution** fixes the requested dimensions throughout the search; leave one dimension blank to preserve proportions. Smart does not upscale beyond the cropped source and rejects pinned resolutions above its 16-megapixel safety limit. **Pin frame rate** fixes the chosen FPS. GIF playback is limited to 50 FPS here; PNG sequence timing also includes the playback-speed multiplier. With both axes pinned, Smart searches quality and permitted encoder effort rather than quietly reducing dimensions or frame rate. If no tested candidate fits, it reports that result and suggests releasing a pin, shortening the clip, or increasing the limit.

Search effort controls the candidate budget. Thorough can also verify Extra encoder effort on a fitting candidate. The search tests changes with substantial size impact before small quality refinements, reuses compatible prepared frames, and stops when tested gains diminish or its budget is spent. It preserves your chosen clip, crop, playback, and image processing; it never silently trims to fit.

The selected result is the best verified candidate under a documented settings preference, not a measured perceptual-quality score or a guarantee of a global optimum. Review the actual output.

### Sharing size shortcuts

Smart includes two convenient limits; both remain editable:

| Shortcut | Target | Guidance |
|---|---|---|
| Linked embed | 10 MB | Conservative compatibility target based on older Discord image/embed guidance; not a verified separate current maximum. |
| Discord upload / preview | 20 MB | Conservative decimal target for Discord’s documented free uploads and direct image previews. |

Discord’s [File Attachments FAQ](https://support.discord.com/hc/en-us/articles/25444343291031-File-Attachments-FAQ), updated September 30, 2026, documents 20 MB free uploads and download-only handling for directly uploaded images above 20 MB. Its older [image/GIF guide](https://support.discord.com/hc/en-us/articles/211866427-How-do-I-upload-images-and-GIFs), updated June 11, still mentions 10 MB for images and embedded media. The 10 MB shortcut is therefore cautious guidance, not a claim that Discord currently enforces distinct 10 MB and 20 MB maxima. Account limits, hosting, and preview settings can affect sharing.

### Estimates and encoding effort

Size estimates are previews: short clips may be measured completely; longer clips use separated short samples and show an uncertainty range. The toolbar labels calculating, cached, stale, and failed states. During Smart searches it distinguishes the latest trial estimate from a completed measured output. Unchanged recipes can reuse cached estimates. Estimates never replace the final byte check.

Gifski exposes exactly three encoding efforts: **Normal** (no effort flag), **Fast** (`--fast`), and **Extra** (`--extra`). Extra prioritizes fidelity and often makes larger files. Fast prioritizes encoding speed and may worsen compression efficiency. Smart’s search effort changes how many recipes are tested; it is a separate setting.

Duration, dimensions, and frame count strongly affect size. Overall quality has independent motion/lossy overrides in Advanced. Noise, gradients, movement, text, and prior compression change the tradeoffs. GIF cannot retain partial alpha, audio, HDR, or full video color depth; rates above 50 FPS cannot be represented reliably by Gifski’s minimum frame delay.

## Controls and pipelines

**Gifski:** overall/motion/lossy quality, normal/fast/extra effort, dimensions, frame rate, speed, repeats, bounce, frame order, fixed colors, matte, Y4M matrix interpretation, quiet output, file naming, help, and version.

**FFmpeg preprocessing:** trim, crop, scaling filter, FPS conversion, speed, reverse, denoise, sharpen, and custom filter chains. Advanced groups image processing, motion/compression/palette, input/diagnostics, performance/hardware, and the command plan. See [FEATURES.md](FEATURES.md) for the complete inventory.

| Pipeline | Use |
|---|---|
| Auto / PNG | RGBA intermediates preserve alpha and allow transformations; temporary frames need disk space. |
| Y4M | Streams opaque frames from FFmpeg to Gifski with less intermediate disk use. |
| Direct | Native PNG/GIF/Y4M input. Conventional video requires Gifski’s optional video-enabled build. FFmpeg transforms are rejected. |

Gifski has no CLI dither-strength, palette-size, thread-count, or resize-kernel slider. FFmpeg scaling controls belong to preprocessing.

## Progress and performance

The activity column displays the process, elapsed time, and available frame, FPS, and speed reports. Percentages describe the current stage; unknown progress stays indeterminate. Quiet processes still get start, completion, and periodic keepalive log entries. A pause in reported output is a diagnostic, not proof that encoding has stopped. Turn off **Follow log** to read earlier output without losing your position.

The browser polls compact history summaries and selected-job details, with slower polling when hidden. Interrupted updates show a retry message. Request deadlines do not themselves cancel an encoder. **Cancel** stops tracked processes, including job metadata inspection, with terminate/kill escalation. History thumbnails prefer a finished output, falling back to a source frame at the trim start for jobs such as estimates. They are static JPEGs within 160 × 90 pixels, limited to 128 KiB and five seconds of preparation. Missing inputs or failed thumbnails leave a placeholder without failing the job.

**Advanced → Performance & hardware** contains:

- **FFmpeg CPU threads:** `0` keeps decoding automatic and caps filter/PNG preparation workers at the smaller of eight and the detected logical CPU count. Values `1–256` request the decoder, filter, and PNG thread counts. Individual codecs may use fewer workers.
- **Video decoding:** CPU is the default. **Hardware when available** requests FFmpeg’s automatic hardware decoder selection, with a CPU retry if the attempt fails. Availability and speed depend on the build, drivers, and source. Built-in image filters and Gifski encoding remain CPU work.

Gifski manages its own CPU parallelism. More threads or hardware decoding need not make a particular clip faster. Direct mode bypasses FFmpeg, so these controls do not affect it.

**Settings → Browser** optionally requests a screen wake lock while a job runs and the page is visible. It is off by default, requires browser support, and does not guarantee that the computer cannot suspend.

## Dependencies and version checks

Explicit paths override automatic detection. Otherwise PATH is checked before the app’s `bin/` folder, with the app root retained as a legacy location. FFprobe may also be found beside configured FFmpeg. An unusable PATH candidate can fall through to a working local copy; an invalid explicit override must be corrected or cleared. Restart the app after changing system PATH, because an already running process retains its launch environment.

The Windows installer uses the official ImageOptim Gifski release and the Gyan FFmpeg build linked from FFmpeg’s download page. Downloads have size limits and published SHA-256 verification, safe selective extraction, and staged replacement. Installation requires an explicit action and does not run provider install scripts. Gifski and FFmpeg have their own licenses; no encoder binaries are tracked in this repository.

**Check for updates** starts asynchronously, refreshes the active installed executables, then compares them with official release metadata. Successful upstream lookups can be reused for 15 minutes while local detection is refreshed. Independent provider results appear as they arrive. Gifski’s release API can fall back to its official release page; FFmpeg’s archive can fall back to stable tags from its official mirror.

Version results show the active path, detection source, installed version, upstream comparison, and applicable update method. Vendor builds with a recognized stable release base can match that upstream release; vendor rebuilds and package revisions remain the provider’s responsibility. Nightly, Git, development, prerelease, and unrecognized builds receive an explicit uncertain comparison rather than a false current/outdated claim. Network failures show specific diagnostics. Local executable refresh is also available separately.

**Installing an app-local update does not replace or switch a working PATH or explicitly configured executable.** Update that installation using its package manager/installer, or explicitly select the downloaded local executable.

## Local data and privacy

- The ordinary launcher binds only to `127.0.0.1`. No analytics, external UI scripts, cloud encoding, or accounts.
- Inputs are not overwritten. Jobs, outputs, and thumbnail caches use unique folders under `data/jobs`; upload copies live under `data/uploads`.
- Executable paths and job history stay local. Saved recipes use browser storage; export them for a portable backup.
- `data/`, `bin/`, caches, virtual environments, and environment files are ignored by Git. Do not commit media, configuration, or diagnostic logs containing local paths.
- Jobs run serially. Cancellation retains finished candidates; restart marks unfinished jobs interrupted. Expected browser disconnects do not trigger a second HTTP response or a traceback.
- Close the app before removing old files under `data/` to reclaim space.
- Custom FFmpeg filters and local paths are trusted local-operator features. Do not expose the server to the internet; development preview mode is only for an isolated preview environment.

## Development

Python standard library plus plain HTML/CSS/JavaScript. Native dialogs and disclosure controls provide settings and estimate details; ResizeObserver reserves toolbar space, AbortController bounds network waits, and screen wake lock is optional.

```sh
python -m unittest discover -v
node --check static/app.js
```

Node is optional and used only for the syntax check. Unit tests use temporary data and controlled downloads. Real encoding validation requires Gifski, FFmpeg, and FFprobe.

| Path | Purpose |
|---|---|
| `server.py` | Local HTTP API and static files |
| `engine.py` | Probing, pipelines, queue, estimates, and optimization |
| `dependencies.py` | Portable installation and version checks |
| `thumbnails.py` | Bounded static history thumbnail cache |
| `static/` | Browser UI |
| `test_*.py` | Engine, HTTP, installer, and thumbnail tests |
| `FEATURES.md` | Gifski feature coverage |
| `DESIGN.md` | Layout philosophy, search strategy, and hardening boundaries |

## References

- [Gifski](https://github.com/ImageOptim/gifski) and [releases](https://github.com/ImageOptim/gifski/releases)
- [FFmpeg](https://ffmpeg.org/download.html) and [filters](https://ffmpeg.org/ffmpeg-filters.html)

Independent interface; not an official ImageOptim release.
