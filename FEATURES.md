# Gifski feature reference

Audited against the published Gifski 1.34.0 command-line source. The app checks the installed executable separately; a different build can expose different capabilities. Source references are below.

## Complete CLI inventory

| Option | Values / default | Meaning |
|---|---|---|
| `--output`, `-o` | Required path; `-` = stdout | GIF destination |
| Positional files | PNG sequence; single Y4M/GIF/video*; `-` = stdin | Source |
| `--fps`, `-r` | Float, >0–100; 20 | Frame rate |
| `--fast-forward` | Float, >0–1000; 1 | Playback multiplier |
| `--quality`, `-Q` | Integer 1–100; 90 | Overall quality |
| `--motion-quality` | Integer 1–100; inherits quality | Temporal detail |
| `--lossy-quality` | Integer 1–100; inherits quality | Lossy compression |
| `--fast` | Boolean; off | Fast effort mode |
| `--extra` | Boolean; off | Extra effort mode; conflicts with fast |
| `--width`, `-W` | Unsigned integer | Maximum width |
| `--height`, `-H` | Unsigned integer | Maximum height |
| `--repeat` | -1 / 0 / 1–32767; 0 | Once / indefinitely / repeats |
| `--bounce` | Boolean; off | Forward/reverse PNG sequence |
| `--no-sort`, `--nosort` | Boolean; off | Preserve supplied order |
| `--fixed-color` | Repeated six-digit RGB hex; optional `#` | Retained palette colors |
| `--matte` | Six-digit RGB hex; optional `#` | Partial-alpha background |
| `--y4m-color-override` | bt709, fcc, bt470bg, bt601, smpte240, ycgco | Y4M matrix |
| `--quiet`, `-q` | Boolean; off | Suppress normal console output |
| `--help`, `-h` | Long / short help | Usage and capabilities |
| `--version`, `-V` | Flag | Version |

*Direct conventional video decoding requires Gifski's optional video build feature. Using the separate FFmpeg executable removes this requirement. PNG input requires multiple images. Fixed colors accept space/comma lists and repeated flags. Long help includes options omitted from short help.

Normal, Fast, and Extra are the three CLI effort modes: Normal omits both effort flags. The UI’s Smart search effort is a separate candidate-search budget, not another Gifski effort level.

## Behavior that affects results

- PNG frame rate sets timing without discarding frames. Video rate resamples motion. Gifski sorts frame paths naturally unless sorting is disabled. Native bounce applies only to individual PNG files; it is ignored for a single video/pipe. The wrapper can implement equivalent behavior before encoding.
- The native encoder never enlarges images. One maximum dimension preserves proportions; two can stretch. Omitting both enables automatic downscaling around an 800 × 600 pixel budget. Explicit processed dimensions avoid a second resize.
- GIF stores centisecond timing. Gifski clamps each written delay to at least 2 centiseconds: settings above 50 FPS cannot produce equally fast GIF playback. Fractional rates produce uneven frame delays.
- Matte blends semitransparent pixels; fully transparent pixels remain transparent. Without matte, alpha is converted to binary transparency. Y4M has no alpha: use RGBA PNG frames for transparent material.
- Fixed colors are capped at 255, reserving one palette entry for transparency. They can increase size. Additional encoding effort may improve appearance while increasing size; it is not a guaranteed compression improvement.

## Quality versus size

Resolution and duration are powerful size controls. Lower lossy quality adds noise/streaks; lower motion quality sacrifices temporal detail. Preserve these as separate controls when testing combinations. Output-size estimates are approximate. A target-size search must measure complete resulting GIF files, keep each candidate's settings, and report when no tested candidate meets the budget. Presets are starting points, not guaranteed optimums.

FFmpeg preprocessing provides trim, crop, scale algorithms, FPS conversion, timestamp/speed adjustment, rotation, reversal, color correction, and denoising. These are separate from Gifski encoding options. The `fps` filter drops/duplicates frames; `setpts` changes timing. Trimming before reversal limits buffering. Mild denoising may help noisy footage compress, but it can remove detail.

## Deliberate boundaries

Gifski's CLI has no independent dithering-strength, palette-count, thread-count, or resize-algorithm option. Do not present invented Gifski flags for these. Its library APIs additionally accept raw RGB/RGBA buffers, per-frame timestamps, and progress/write callbacks; these are integration interfaces, not extra CLI sliders. The optional lossy-compression build feature can affect whether lossy-quality changes have an effect. This wrapper uses local executables, not a recompiled library.

## Official references

- [Published CLI source, Gifski 1.34.0](https://docs.rs/crate/gifski/1.34.0/source/src/bin/gifski.rs)
- [Encoder implementation](https://docs.rs/crate/gifski/1.34.0/source/src/lib.rs)
- [PNG timing](https://docs.rs/crate/gifski/1.34.0/source/src/bin/png.rs)
- [Y4M decoding](https://docs.rs/crate/gifski/1.34.0/source/src/bin/y4m_source.rs)
- [Upstream usage and size guidance](https://github.com/ImageOptim/gifski)
- [C API](https://github.com/ImageOptim/gifski/blob/main/gifski.h)
- [Release downloads](https://github.com/ImageOptim/gifski/releases)
- [FFmpeg filter reference](https://ffmpeg.org/ffmpeg-filters.html)

## WebUI decision tools

- **Workspace:** Default, Smart, Discord, Other presets, Custom, and Advanced share the left-hand controls. Process status, logs, results, and history stay in the right column; Settings opens a dialog. The fixed footer holds output/estimate summaries and the encode action. The layout stacks on smaller screens. There are no Create, History, Compare, or Fit a size navigation tabs.
- **Source matching:** width/height/FPS and trim-end controls can follow probed values. Matching a dimension clears the other dimension to preserve aspect ratio. FPS matching caps at 50 FPS.
- **Size estimate:** full measurement for clips up to two seconds; three separated samples of about half a second for longer clips (at least four frames each). Shows a heuristic range in expandable footer details, labels cached/stale/calculating/error states, and does not scroll to a separate estimate panel. Unchanged recipes reuse a five-minute memory cache keyed by settings, source metadata, and executables. Samples are never counted as verified exports. Unknown duration needs a trim end; long native-direct input requires preprocessing to sample.
- **Smart and Discord:** share an adaptive search. Smart takes a decimal MB limit; Discord fixes it at 20,000,000 bytes. Start at source dimensions (up to a 16-megapixel safety bound) and source timing (up to 50 FPS), preserving aspect ratio. Probe quality, width, and FPS separately to estimate their size impact; screen promising combinations before complete verification. Effort budgets are Quick (5 sample recipes / 4 full encodes), Balanced (9 / 8), and Thorough (15 / 12); each can stop earlier. Thorough can test Extra encoder effort on a fitting result. Sample recipes each use up to three short encodes.
- **Ranking:** width retention has weight 0.75 and FPS retention 0.30; quality uses a diminishing-return curve near 100. Extra receives a small preference bonus when its full output fits. A predicted gain below 0.8% can stop refinement. This is a documented settings preference, not a measured perceptual metric or an exhaustive optimum. Source, chosen trim, crop, image filters, and playback remain intact; the search never silently trims to meet a limit.
- **Verification and resources:** only full outputs at or below the exact byte limit qualify. The best verified candidate is kept on cancellation; no-fit is explicit. Compatible PNG preprocessing is reused within a job, capped at 512 MiB cached / 256 MiB per entry, then removed. Extraction rejects more than 100,000 frames rather than returning a truncated export.
- **Live process feedback:** stage progress, elapsed time, and available frame/FPS/speed information. Structured FFmpeg progress and meaningful Gifski PNG frame counts drive percentages; unknown progress stays indeterminate. Carriage-return console output is handled continuously. Bounded logs include process start/exit and periodic keepalives even in quiet mode; follow-log can be disabled. Cancel terminates tracked processes with kill escalation. Long gaps in output produce a diagnostic, not a fabricated failure.
- **Efficient updates:** compact history summaries plus selected-job details, non-overlapping requests with deadlines, slower hidden-page polling, visible reconnect feedback, and stable history nodes/log scroll. A pending estimate can continue updating the footer while another job is inspected.
- **CPU and hardware:** Advanced offers FFmpeg threads `0–256` and CPU or automatic hardware video decoding. Zero keeps decoder selection automatic and bounds filter/PNG workers to eight or the logical CPU count, whichever is lower. Explicit thread requests apply to FFmpeg decoding, filters, and PNG preparation. Hardware decoding is optional and can retry on the CPU; filters and Gifski remain CPU work. Gifski manages its own threading. FFmpeg controls have no effect in Direct mode.
- **Browser enhancement:** an optional screen wake lock during active jobs, only while visible and supported. Disabled or rejected wake lock does not block encoding; it is not a system-suspend guarantee.
- **Dependency setup:** explicit paths override automatic detection; otherwise PATH precedes the app-local copy. FFprobe can also be found beside configured FFmpeg. Active versions, paths, and detection origins are visible. Windows x64 **Set up missing tools** checks existing executables and downloads only missing dependencies into `bin/`, with fixed providers and SHA-256 checks. Downloads require an install action. Existing PATH tools are retained; Python must already be installed. Installs and path changes cannot overlap active exports.
- **Version checks:** on-demand comparison of active executables against upstream release metadata, a 15-minute successful-result cache, and uncertain states for custom/nightly builds. Gifski’s API can fall back to its official release page; FFmpeg’s archive can fall back to stable tags from its official mirror. Provider results arrive independently. The UI reports the source used and specific connection, certificate, proxy, rate-limit, access, timeout, or metadata errors. A portable update does not override a working PATH executable or explicit path.

The older sweep/optimize API modes remain readable for existing history and integrations; they have no separate interface workspace. See [DESIGN.md](DESIGN.md) for the layout philosophy and boundaries.
