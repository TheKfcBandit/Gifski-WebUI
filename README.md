# Gifski WebUI

A local browser interface for Gifski and FFmpeg. Tune every Gifski CLI encoding option, compare real outputs, estimate file size, and search for a GIF that fits a size limit.

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

1. **Choose a source.** Pick a video or PNG sequence, drag files into the source area, or use a local path to avoid making an upload copy. FFprobe reports source dimensions, duration, and frame rate.
2. **Frame and time it.** Trim, crop, and choose dimensions/FPS. Match-source controls copy the corresponding source values. Leave one dimension blank to preserve aspect ratio. Setting both can stretch.
3. **Choose a preset.** Start with Balanced, Compact, Fidelity, UI & text, Motion, Transparent, or Discord. Save custom recipes and export/import JSON backups.
4. **Estimate or encode.** Size estimation uses real sample encodes and shows a range. A sample estimate is not a file-size guarantee; an exact short-clip measurement is identified separately.
5. **Compare.** Sweep overall quality, dimensions, FPS, and independent motion/lossy quality settings. Pin results for side-by-side viewing and inspect actual byte counts, settings, and commands.
6. **Fit a limit.** Target-size search uses complete encodes. It keeps the candidates and clearly reports when none meet the budget.

### Discord: 20 MB maximum

The Discord preset derives its starting dimensions and frame rate from the source, then searches a bounded set of resolution, FPS, and quality candidates. The cap is **20,000,000 bytes**, not 20 MiB. Only a fully rendered GIF at or below that byte count is marked as fitting.

This is a configurable workflow preset, not a claim about every Discord account's current upload limit. A file may still need smaller dimensions, a shorter duration, or a larger search budget. The selected candidate is the best found by the documented search preference, not a measured perceptual optimum.

### Understand quality and size

- Duration, dimensions, and frame count are powerful size controls. Trim unnecessary content before lowering quality.
- Overall quality sets the baseline. Independent motion and lossy overrides let you examine different kinds of degradation.
- `Extra` prioritizes fidelity and often makes larger files. `Fast` prioritizes encoding speed and may worsen compression efficiency.
- No universal preset maximizes perceptual quality per byte. Noise, gradients, movement, text, and previous compression change the tradeoff. Review the actual GIFs.
- A sampled estimate can miss complex scenes and does not establish the final size. Final budget checks use complete encodes.
- GIF has binary transparency and centisecond timing. It cannot retain partial alpha, audio, HDR, or full video color depth. Rates above 50 FPS cannot be represented reliably by the encoder's minimum frame delay.

## Controls and pipelines

**Gifski:** overall/motion/lossy quality, normal/fast/extra effort, width/height, frame rate, speed, repeats, bounce, frame order, fixed palette colors, matte, Y4M matrix interpretation, quiet output, file naming, help, and version.

**FFmpeg preprocessing:** trim, crop, scaling filter, FPS conversion, speed, reverse, denoise, sharpen, and custom filter chains. Advanced controls are grouped into expandable sections. See [FEATURES.md](FEATURES.md) for the option-by-option inventory and limitations.

| Pipeline | Use |
|---|---|
| Auto / PNG | Lossless RGBA intermediates; alpha and transformations; temporary frames need disk space. |
| Y4M | Stream opaque frames from FFmpeg to Gifski with less intermediate disk use. |
| Direct | Native PNG/GIF/Y4M input. Conventional video requires Gifski's optional video-enabled build. FFmpeg transforms are rejected in this mode. |

Gifski has no CLI dither-strength, palette-size, thread-count, or resize-kernel slider. FFmpeg's scaling controls are preprocessing, not invented Gifski flags.

## Dependency downloads

The installer uses fixed trusted sources: the official ImageOptim Gifski release and the Gyan FFmpeg release build linked from FFmpeg's download page. Archives are size-limited, checked against published SHA-256 information, and extracted only for the required executables. Source URLs and installation progress are visible. If verification or installation fails, choose installed executable paths instead. Downloads do not run arbitrary install scripts.

Gifski and FFmpeg are separate programs with their own licenses. No encoder binaries are tracked in this repository.

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
| `dependencies.py` | Portable dependency installation |
| `static/` | Browser UI |
| `test_*.py` | Engine, HTTP, and installer tests |
| `FEATURES.md` | Gifski feature coverage |

## References

- [Gifski](https://github.com/ImageOptim/gifski) and [releases](https://github.com/ImageOptim/gifski/releases)
- [FFmpeg](https://ffmpeg.org/download.html) and [filters](https://ffmpeg.org/ffmpeg-filters.html)

Independent interface; not an official ImageOptim release.
