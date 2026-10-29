# Gifski WebUI design

## Product philosophy

The task is to produce a good GIF. The interface follows the decisions needed for that task: source, preset, adjustments, output. Choosing a size limit belongs beside choosing a preset; it does not require a separate workspace. A complete result's actual byte count is the authority for whether it fits.

Use progressive disclosure. Default settings should be useful without reading an encoder manual. Custom exposes ordinary adjustments; Advanced exposes every CLI control in clearly named groups. Smart and Discord manage dimensions, frame rate, and encoder quality, so those manual fields do not compete with the automated search. Common clipping and playback choices remain available.

Every preset uses the same button treatment. Discord is simply Smart with a fixed 20 MB limit. Other presets and saved recipes share one place. Setup only establishes working dependencies; it does not ask the user to pick an export preset. History and live activity stay beside the controls, so inspecting progress does not require leaving the recipe. Settings is a dialog for occasional configuration.

## Layout plan

The desktop workspace has two columns. The left contains source and recipe controls; the right is a persistent, independently scrollable activity column. It stacks below the controls on smaller screens. The bottom action toolbar stays visible, and its measured height reserves space so it does not cover page content.

1. **Source, left:** file/path choice, source preview, metadata, and change-source action.
2. **Preset, left:** Default, Smart, Discord, Other presets, Custom, Advanced. Show the relevant description and controls directly below the choice.
3. **Adjustments, left:** clipping and playback, output name, a size limit/search effort for Smart, or manual quality controls. Advanced expands image processing, palette/compression, pipeline/diagnostics, performance/hardware, and the command plan. Source matching sits beside its field.
4. **Action toolbar:** output summary, estimate summary, and one primary **Encode GIF** or **Find best fit** action. Ordinary recipes also offer **Estimate size**. Estimate details open above the toolbar; sample estimates do not add a results panel or scroll the page. Stale and cached estimates are labeled.
5. **Activity, right:** current process, stage progress, elapsed time, available frame/rate information, cancellation, and a visible process log. Follow-log is optional. Show an indeterminate state when progress cannot be measured rather than inventing a percentage.
6. **Output and history, right:** selected full GIF, actual size, recipe, download, and collapsed alternatives; recent jobs stay in the same column. Selecting history updates the inspection panel without page scrolling or replacing focused history controls on every poll. No Create/History navigation tabs or comparison workspace.
7. **Settings dialog:** detected dependency versions and origins, active executable paths, explicit online version check, portable installation, CLI help, and an optional browser screen wake lock. First launch opens dependency setup only when needed. Setup does not choose a preset or download until an install action is chosen.

## Responsive process feedback

FFmpeg reports structured progress. Gifski’s carriage-return output is read continuously and sanitized; PNG frame counts can supply a meaningful percentage, while ambiguous Y4M output stays indeterminate. Start/exit messages and periodic keepalives remain available even with quiet encoder output. A long gap in reported output is labeled as uncertain activity, not automatically treated as a failed encode. The wrapper drains both sides of a pipe and terminates a waiting peer when its producer/consumer fails.

Poll compact history summaries, then details only for the selected job and any separately pending size estimate. Slow polling in hidden documents. Requests have deadlines, overlapping polls are prevented, and interrupted updates show a retry message. Reuse history nodes and leave unchanged logs and result previews intact. A request timeout or tab visibility change does not cancel an encoder.

Screen wake lock is an opt-in browser enhancement. Request it only while a job is active and the document is visible; release it when no longer needed and handle unsupported or rejected requests. This is not a guarantee against system suspension. The app remains usable without it.

## Performance controls

Keep the distinction between decoding, image preparation, and GIF encoding visible. Optional FFmpeg hardware decoding depends on the source, build, and driver, and can retry on the CPU. It does not move the built-in filters or Gifski encoder to the GPU. FFmpeg thread settings apply to its decoder, filters, and PNG output; automatic filter/PNG workers are bounded to eight or the detected CPU count, whichever is lower. Gifski keeps its native CPU parallelism with no invented CLI thread slider. Direct input bypasses FFmpeg.

## Search principles

- Preserve the selected duration, aspect ratio, and intended playback. Never silently trim to fit.
- Start from source detail and motion without upscaling; respect explicit processing and resource limits.
- Screen representative temporal samples before paying for full exports. Cache unchanged estimates and reuse compatible prepared frames. Samples are guidance, never confirmed fits.
- Explore meaningful resolution/frame-rate changes and moderate quality changes, then refine near promising candidates. This avoids exhaustive small increments across every parameter.
- Prefer retained spatial detail, useful motion, and high encoder quality using an explicit settings score. Thorough can spend remaining budget verifying Extra encoder effort. That score is a heuristic, not a measured perceptual metric. Content can defeat any universal ranking.
- Verify shortlisted candidates with full encodes. Only actual byte counts qualify. Stop when improvement is small or the chosen effort budget is spent; report no-fit honestly.
- Bound memory, disk, sampling, candidate counts, and cancellation. Prefer a clear failure to a silently truncated export.

## Hardening boundaries

Ordinary launch binds to loopback. API requests require a browser session and matching origin/host; source previews only serve registered media. Request bodies have explicit limits, finite JSON values, unambiguous framing, and read deadlines. Rejected bodies close the connection. Request query strings are not logged. Expected client disconnects, including Windows aborted connections, do not trigger a second response or a server traceback.

Encoder processes receive argument arrays without a shell. Output is drained while they run, logs are bounded, and cancellation uses terminate/kill escalation for tracked processes. Executable probes run outside the job-state lock and share a single refresh operation. Input media is never overwritten. Downloads use fixed HTTPS providers, size limits, SHA-256 checks, safe selective extraction, and staged replacement. Version lookup is explicit and cached, with independent provider checks, fixed official metadata fallbacks, bounded reads, and actionable failure diagnostics. No background update installation occurs. Dependency detection prioritizes explicit configuration, then PATH, then app-local copies; FFprobe may be discovered alongside configured FFmpeg. Missing-only setup rechecks availability before downloading. Installing a local copy does not silently override a working PATH tool or explicit path.

Local filters and executable selection are trusted-operator capabilities. This is a local application, not a multi-user service; custom FFmpeg filters can access resources available to FFmpeg. Do not expose it to untrusted networks. Temporary media, config, caches, and logs are excluded from Git.

## Validation

Engine tests cover candidate ranking, actual-size acceptance, estimation, resource limits, and cancellation. HTTP tests cover session/origin restrictions, request framing, malformed input, file access, uploads, and media ranges. Dependency tests use controlled downloads and archive fixtures. CI runs on Windows and Linux with Python 3.10, 3.13, and 3.14; JavaScript syntax is checked separately. Browser QA should exercise the complete preset-to-export path, responsive layout, log following, history selection, toolbar estimate states, and dependency diagnostics. A test matrix is not a claim that every external encoder build or platform has been manually tested.

Performance observations depend on the source, encoder builds, CPU/GPU, and chosen settings. Use representative clips and measured output sizes when evaluating changes; cached estimates and synthetic clips do not establish general speed or quality guarantees.
