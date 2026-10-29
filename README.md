# Gifski WebUI

A local browser interface for Gifski and FFmpeg. Choose a preset, estimate file size, or let Smart find a high-quality GIF within your size limit. Advanced mode exposes every Gifski CLI encoding option.

## Start

### Windows

1. Download the repository ZIP and extract it, or clone the repository.
2. Install **Python 3.10 or newer** from [python.org](https://www.python.org/downloads/) if needed.
3. Double-click **Start-Windows.cmd**. Keep its terminal open.
4. Follow the setup wizard. It detects Gifski, FFmpeg, and FFprobe already on PATH or inside `bin/`. You can also enter executable paths in **System**.
5. On Windows x64, the wizard can download portable Gifski and FFmpeg into the app's `bin/` folder. No administrator access or PATH changes are needed. FFprobe is included with FFmpeg.

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
4. **Create the GIF.** Ordinary presets encode directly. Smart and Discord screen short samples, then encode promising candidates in full. The selected output, measured size, and download are shown first; other verified attempts are available in a collapsed list.

History and dependency settings are secondary views. There is no separate comparison workspace or size-fitting tab. Saved presets can be imported/exported as JSON.

### Smart and Discord

Smart uses a customizable **decimal MB** limit. Discord uses exactly **20,000,000 bytes**, not 20 MiB. Both use the same search engine and keep trimming, crop, playback, and image-processing choices. A limit is satisfied only when a complete resulting GIF is measured at or below it.

Search effort controls the time budget; Thorough can also verify Extra encoder effort on a fitting candidate. Representative short samples screen candidate settings before full verification; samples and reusable preprocessing are cached during a session. The search tests significant spatial and temporal changes before spending time on small quality refinements. It stops when further tested improvements are negligible or its budget is exhausted. No search shortens your chosen clip to fit.

The selected result is the best verified candidate under the documented settings preference, not a perceptual-quality measurement or a guarantee of a global optimum. A no-fit result is explicit. Difficult clips may require a smaller resolution, a shorter duration, or a larger limit. Discord is a convenient 20 MB preset, not a claim about every account's upload allowance.

Size estimates are previews: short clips may be measured completely; longer clips use separated short samples and display an uncertainty range. Unchanged recipes can reuse cached estimates. Estimates never replace the final byte check.

### Understand quality and size

- Duration, dimensions, and frame count are powerful size controls. Trim unnecessary content before lowering quality.
- Overall quality sets the baseline. Independent motion and lossy overrides let you examine different kinds of degradation.
- `Extra` prioritizes fidelity and often makes larger files. `Fast` prioritizes encoding speed and may worsen compression efficiency.
- No universal preset maximizes perceptual quality per byte. Noise, gradients, movement, text, and previous compression change the tradeoff. Review the actual GIFs.
- A sampled estimate can miss complex scenes and does not establish the final size. Final budget checks use complete encodes.
- GIF has binary transparency and centisecond timing. It cannot retain partial alpha, audio, HDR, or full video color depth. Rates above 50 FPS cannot be represented reliably by the encoder's minimum frame delay.

## Controls and pipelines

**Gifski:** overall/motion/lossy quality, normal/fast/extra effort, width/height, frame rate, speed, repeats, bounce, frame order, fixed palette colors, matte, Y4M matrix interpretation, quiet output, file naming, help, and version.

**FFmpeg preprocessing:** trim, crop, scaling filter, FPS conversion, speed, reverse, denoise, sharpen, and custom filter chains. Advanced controls are grouped by framing, image processing, encoder quality, color, playback, and pipeline. See [FEATURES.md](FEATURES.md) for the option-by-option inventory and limitations.

| Pipeline | Use |
|---|---|
| Auto / PNG | Lossless RGBA intermediates; alpha and transformations; temporary frames need disk space. |
| Y4M | Stream opaque frames from FFmpeg to Gifski with less intermediate disk use. |
| Direct | Native PNG/GIF/Y4M input. Conventional video requires Gifski's optional video-enabled build. FFmpeg transforms are rejected in this mode. |

Gifski has no CLI dither-strength, palette-size, thread-count, or resize-kernel slider. FFmpeg's scaling controls are preprocessing, not invented Gifski flags.

## Dependency downloads

The installer uses fixed trusted sources: the official ImageOptim Gifski release and the Gyan FFmpeg release build linked from FFmpeg's download page. Archives are size-limited, checked against published SHA-256 information, and extracted only for the required executables. Source URLs and installation progress are visible. If verification or installation fails, choose installed executable paths instead. Downloads do not run arbitrary install scripts.

Gifski and FFmpeg are separate programs with their own licenses. No encoder binaries are tracked in this repository.

### Version checks

**Settings → Check updates** compares the active Gifski, FFmpeg, and FFprobe versions with official stable releases. Checks run only when requested and are cached for 15 minutes. Nightly or vendor-specific versions are reported as uncertain when they cannot be compared reliably. Installing a portable update does not replace a separately configured executable; clear that path to use the portable copy.

## Local data and privacy

- The ordinary launcher listens only on `127.0.0.1`. No analytics, external UI scripts, cloud encoding, or accounts.
- Inputs are not overwritten. Jobs and generated files use unique folders under `data/jobs`; upload copies live under `data/uploads`.
- Executable paths and job history remain local. Presets are in browser storage; export them for a portable backup.
- `data/`, `bin/`, caches, virtual environments, and environment files are ignored by Git. Do not commit source media, configuration, or diagnostic logs containing local paths.
- Jobs run serially. Cancel stops encoding and retains finished candidates. Restart marks unfinished jobs interrupted.
- Close the app before removing old files under `data/` to reclaim space.
- Custom FFmpeg filters and local paths are for the local operator. Do not expose the server to the internet. Development preview options are intended only for an isolated preview environment.

## Development

The app uses Python's standard library and plain HTML/CSS/JavaScript.

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
