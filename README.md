# Gifski WebUI

A local browser interface for Gifski and FFmpeg. Choose a preset, estimate file size, or let Smart find a high-quality GIF within your size limit. Advanced mode exposes every Gifski CLI encoding option.

## Start

### Windows

1. Download the repository ZIP and extract it, or clone the repository.
2. Install **Python 3.10 or newer** from [python.org](https://www.python.org/downloads/) if needed.
3. Double-click **Start-Windows.cmd**. Keep its terminal open.
4. Follow the setup wizard. Automatic detection checks PATH before the app’s `bin/` folder. **Settings** shows each active executable, version, and detection source; an explicit path overrides automatic detection.
5. On Windows x64, choose **Set up missing tools** to download only missing dependencies into the app’s `bin/` folder. Existing PATH tools are kept. Downloads start only after an install action; no administrator access or PATH changes are needed. FFprobe is included with FFmpeg.

The browser opens at `http://127.0.0.1:8765`. No pip packages, Node.js, account, or web build is required. Installing dependencies needs an internet connection; ordinary encoding stays local.

### macOS / Linux

Install Python 3.10+, Gifski, FFmpeg, and FFprobe with your preferred package manager, then run:

```sh
sh start.sh
```

Automatic portable installation is Windows x64 only. The setup wizard gives manual guidance for other systems. To use another port, run `python3 server.py --port 8766` (Windows: `py -3 server.py --port 8766`).

## Workflow

1. **Choose a source.** Drop a video or PNG sequence, choose files, or enter a local path to avoid making an upload copy.
2. **Choose a preset.** Default is a balanced starting point. Smart accepts your file-size limit; Discord fixes it at 20 MB. Other presets contains Compact, Fidelity, UI & text, Motion, Transparent, and saved recipes. Custom exposes everyday tuning; Advanced exposes all encoder and preprocessing options.
3. **Set the clip.** Keep the full source or adjust trimming. Width, height, FPS, and trim end have source-match controls where applicable.
4. **Encode.** Use **Encode GIF** for an ordinary preset or **Find best fit** for Smart and Discord. The latter screen short samples, then encode promising candidates in full. The selected output, measured size, and download appear in the right-hand activity column; other tested outputs are available in a collapsed list.

Controls stay on the left. Process status, logs, results, and history share the right column; the layout stacks on narrow screens. The fixed bottom toolbar holds the output summary, size estimate, and encode actions. Estimate details expand above the toolbar, without scrolling the page. Settings opens a dialog. There are no Create, History, comparison, or size-fitting navigation tabs. Saved presets can be imported/exported as JSON.

### Smart and Discord

Smart uses a customizable **decimal MB** limit. Discord uses exactly **20,000,000 bytes**, not 20 MiB. Both use the same search engine and keep trimming, crop, playback, and image-processing choices. A limit is satisfied only when a complete resulting GIF is measured at or below it.

Search effort controls the time budget; Thorough can also verify Extra encoder effort on a fitting candidate. Representative short samples screen candidate settings before full verification; samples and reusable preprocessing are cached during a session. The search tests significant spatial and temporal changes before spending time on small quality refinements. It stops when further tested improvements are negligible or its budget is exhausted. No search shortens your chosen clip to fit.

The selected result is the best verified candidate under the documented settings preference, not a perceptual-quality measurement or a guarantee of a global optimum. A no-fit result is explicit. Difficult clips may require a smaller resolution, a shorter duration, or a larger limit. Discord is a convenient 20 MB preset, not a claim about every account's upload allowance.

Size estimates are previews: short clips may be measured completely; longer clips use separated short samples and display an uncertainty range. The toolbar shows calculating, cached, stale, or failed states. During Smart and Discord searches, it distinguishes the latest trial estimate from a completed measured output. Unchanged recipes can reuse cached estimates. Estimates never replace the final byte check.

### Understand quality and size

- Duration, dimensions, and frame count are powerful size controls. Trim unnecessary content before lowering quality.
- Overall quality sets the baseline. Independent motion and lossy overrides let you examine different kinds of degradation.
- Gifski exposes exactly three effort choices: **Normal** (no effort flag), **Fast** (`--fast`), and **Extra** (`--extra`). Extra prioritizes fidelity and often makes larger files; Fast prioritizes encoding speed and may worsen compression efficiency. Search effort is separate: it changes how many candidate recipes Smart can test.
- No universal preset maximizes perceptual quality per byte. Noise, gradients, movement, text, and previous compression change the tradeoff. Review the actual GIFs.
- A sampled estimate can miss complex scenes and does not establish the final size. Final budget checks use complete encodes.
- GIF has binary transparency and centisecond timing. It cannot retain partial alpha, audio, HDR, or full video color depth. Rates above 50 FPS cannot be represented reliably by the encoder's minimum frame delay.

## Controls and pipelines

**Gifski:** overall/motion/lossy quality, normal/fast/extra effort, width/height, frame rate, speed, repeats, bounce, frame order, fixed palette colors, matte, Y4M matrix interpretation, quiet output, file naming, help, and version.

**FFmpeg preprocessing:** trim, crop, scaling filter, FPS conversion, speed, reverse, denoise, sharpen, and custom filter chains. Advanced controls are grouped into image processing, motion/compression/palette, input/diagnostics, performance/hardware, and the command plan. See [FEATURES.md](FEATURES.md) for the option-by-option inventory and limitations.

| Pipeline | Use |
|---|---|
| Auto / PNG | Lossless RGBA intermediates; alpha and transformations; temporary frames need disk space. |
| Y4M | Stream opaque frames from FFmpeg to Gifski with less intermediate disk use. |
| Direct | Native PNG/GIF/Y4M input. Conventional video requires Gifski's optional video-enabled build. FFmpeg transforms are rejected in this mode. |

Gifski has no CLI dither-strength, palette-size, thread-count, or resize-kernel slider. FFmpeg's scaling controls are preprocessing, not invented Gifski flags.

## Progress and performance

The right column displays the current process, elapsed time, and available frame, FPS, and speed reports. Percentages describe the current stage. FFmpeg supplies structured progress; Gifski frame percentages are used when available and meaningful. Otherwise the progress bar stays indeterminate. A quiet process still gets start, completion, and periodic keepalive log entries. A pause in reported progress is shown as a diagnostic, not proof that encoding has stopped. Turn off **Follow log** to read earlier output without losing your position.

The browser polls compact history summaries and details for the selected job, with slower polling when hidden. Network failures show a retry message rather than leaving a frozen status unexplained. Browser-request timeouts do not themselves cancel an encoder. Cancel explicitly stops the job’s tracked processes; the wrapper escalates termination if a process does not exit.

**Advanced → Performance & hardware** contains:

- **FFmpeg CPU threads:** `0` selects automatic decoding and caps filter/PNG preparation workers at the smaller of eight and the detected logical CPU count. Values `1–256` set FFmpeg’s requested decoder, filter, and PNG preparation thread counts. Individual codecs may use fewer workers.
- **Video decoding:** CPU is the default. **Hardware when available** requests FFmpeg’s automatic hardware decoder selection for video input, with a CPU retry if the hardware-assisted attempt fails. Availability and speed depend on the installed FFmpeg build, drivers, and source format. The built-in image filters and Gifski encoding remain on the CPU; this is not GPU GIF encoding.

Gifski manages its own CPU parallelism and exposes no CLI thread-count setting. More threads or hardware decoding need not make a particular clip faster. Direct input bypasses FFmpeg, so FFmpeg performance controls do not affect it.

**Settings → Browser** optionally requests a screen wake lock while a job is active and the page is visible. It is off by default, depends on browser support and permission, and does not guarantee that the computer cannot suspend. No wake lock is required for encoding.

## Dependency downloads

With no explicit path, tool detection checks PATH and then the app’s `bin/` folder (with the app root retained as a legacy location). FFprobe can also be found beside an explicitly configured FFmpeg. An unusable PATH candidate can fall through to a working local copy. An explicit executable path is an override; an invalid override must be corrected or cleared. Installing a local update does not replace a working PATH installation. Select the local executable explicitly if you want to use it instead.

The installer uses fixed trusted sources: the official ImageOptim Gifski release and the Gyan FFmpeg release build linked from FFmpeg's download page. Archives are size-limited, checked against published SHA-256 information, and extracted only for the required executables. Source URLs and installation progress are visible. If verification or installation fails, choose installed executable paths instead. Downloads do not run arbitrary install scripts.

Gifski and FFmpeg are separate programs with their own licenses. No encoder binaries are tracked in this repository.

### Version checks

**Settings → Check for updates** compares the active Gifski, FFmpeg, and FFprobe versions with official upstream release metadata. Checks start only when requested and successful checks are cached for 15 minutes. Gifski’s release API can fall back to its official release page; FFmpeg’s release archive can fall back to stable tags from its official GitHub mirror. Provider results appear independently, so one failed lookup does not hide another tool’s result.

The checker shows the source used and specific diagnostics for DNS, HTTPS/certificate, proxy, timeout, rate-limit, access, or metadata failures. Nightly or vendor-specific versions are reported as uncertain when they cannot be compared reliably. **Refresh installed versions** rechecks local executables separately from the online update check. Portable installation does not silently switch an explicit path or working PATH tool.

## Local data and privacy

- The ordinary launcher listens only on `127.0.0.1`. No analytics, external UI scripts, cloud encoding, or accounts.
- Inputs are not overwritten. Jobs and generated files use unique folders under `data/jobs`; upload copies live under `data/uploads`.
- Executable paths and job history remain local. Presets are in browser storage; export them for a portable backup.
- `data/`, `bin/`, caches, virtual environments, and environment files are ignored by Git. Do not commit source media, configuration, or diagnostic logs containing local paths.
- Jobs run serially. Cancel stops encoding and retains finished candidates. Restart marks unfinished jobs interrupted. A browser canceling a media request is treated as a disconnected client, without attempting a second error response.
- Close the app before removing old files under `data/` to reclaim space.
- Custom FFmpeg filters and local paths are for the local operator. Do not expose the server to the internet. Development preview options are intended only for an isolated preview environment.

## Development

The app uses Python's standard library and plain HTML/CSS/JavaScript. Native dialogs and disclosure controls provide the settings and estimate panels; ResizeObserver reserves toolbar space, AbortController bounds network waits, and screen wake lock is optional.

```sh
python -m unittest discover -v
node --check static/app.js
```

Node is optional and only needed for the JavaScript syntax check. Unit tests use temporary data and mocked downloads. Real encoding validation requires Gifski, FFmpeg, and FFprobe.

| Path | Purpose |
|---|---|
| `server.py` | Local HTTP API and static files |
| `engine.py` | Probing, pipelines, queue, estimates, and optimization |
| `dependencies.py` | Portable installation and on-demand version checks |
| `static/` | Browser UI |
| `test_*.py` | Engine, HTTP, and installer tests |
| `FEATURES.md` | Gifski feature coverage |
| `DESIGN.md` | Layout philosophy, search strategy, and hardening boundaries |

## References

- [Gifski](https://github.com/ImageOptim/gifski) and [releases](https://github.com/ImageOptim/gifski/releases)
- [FFmpeg](https://ffmpeg.org/download.html) and [filters](https://ffmpeg.org/ffmpeg-filters.html)

Independent interface; not an official ImageOptim release.
