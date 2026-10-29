# Gifski WebUI design

## Product philosophy

The task is to produce a good GIF. The interface follows the decisions needed for that task: source, preset, adjustments, output. Choosing a size limit belongs beside choosing a preset; it does not require a separate workspace. A complete result's actual byte count is the authority for whether it fits.

Use progressive disclosure. Default settings should be useful without reading an encoder manual. Custom exposes ordinary adjustments; Advanced exposes every CLI control in clearly named groups. Smart and Discord manage dimensions, frame rate, and encoder quality, so those manual fields do not compete with the automated search. Common clipping and playback choices remain available.

Every preset uses the same button treatment. Discord is simply Smart with a fixed 20 MB limit. Other presets and saved recipes share one place. Setup only establishes working dependencies; it does not ask the user to pick an export preset. History and Settings are secondary destinations.

## Layout plan

1. **Source:** file/path choice, source preview, metadata, and change-source action.
2. **Preset:** Default, Smart, Discord, Other presets, Custom, Advanced. Display the relevant description and controls directly below the choice.
3. **Adjustments:** clip range and output settings; a size limit and search effort for Smart; logically grouped encoder controls for Advanced. Source matching sits beside the affected field.
4. **Action and progress:** one primary Create action, a separate lightweight estimate only where useful, then current stage and cancellation.
5. **Output:** best verified GIF, actual size, recipe, and download. Alternative verified attempts are tucked away for inspection. No comparison chart, pin controls, or separate comparison tab.
6. **Settings:** detected dependency versions, explicit update check, executable paths, portable installation, and diagnostic help. First launch opens dependency setup only when needed.

## Search principles

- Preserve the selected duration, aspect ratio, and intended playback. Never silently trim to fit.
- Start from source detail and motion without upscaling; respect explicit processing and resource limits.
- Screen representative temporal samples before paying for full exports. Cache unchanged estimates and reuse compatible prepared frames. Samples are guidance, never confirmed fits.
- Explore meaningful resolution/frame-rate changes and moderate quality changes, then refine near promising candidates. This avoids exhaustive small increments across every parameter.
- Prefer retained spatial detail, useful motion, and high encoder quality using an explicit settings score. Thorough can spend remaining budget verifying Extra encoder effort. That score is a heuristic, not a measured perceptual metric. Content can defeat any universal ranking.
- Verify shortlisted candidates with full encodes. Only actual byte counts qualify. Stop when improvement is small or the chosen effort budget is spent; report no-fit honestly.
- Bound memory, disk, sampling, candidate counts, and cancellation. Prefer a clear failure to a silently truncated export.

## Hardening boundaries

Ordinary launch binds to loopback. API requests require a browser session and matching origin/host; source previews only serve registered media. Request bodies have explicit limits, finite JSON values, unambiguous framing, and read deadlines. Rejected bodies close the connection. Request query strings are not logged.

Encoder processes receive argument arrays without a shell. Input media is never overwritten. Downloads use fixed HTTPS providers, size limits, SHA-256 checks, safe selective extraction, and staged replacement. Version lookup is explicit and cached; no background update installation occurs. User-configured executable paths remain under the user's control.

Local filters and executable selection are trusted-operator capabilities. This is a local application, not a multi-user service; custom FFmpeg filters can access resources available to FFmpeg. Do not expose it to untrusted networks. Temporary media, config, caches, and logs are excluded from Git.

## Validation

Engine tests cover candidate ranking, actual-size acceptance, estimation, resource limits, and cancellation. HTTP tests cover session/origin restrictions, request framing, malformed input, file access, uploads, and media ranges. Dependency tests use controlled downloads and archive fixtures. CI runs on Windows and Linux with Python 3.10, 3.13, and 3.14; JavaScript syntax is checked separately. Browser QA exercises the complete preset-to-export path and responsive layout. A test matrix is not a claim that every external encoder build or platform has been manually tested.

A local benchmark using a generated 12-second, 320 × 180, 20 FPS test clip reduced estimation from 1.87 s to 0.91 s, with a cached repeat taking 0.10 s. Quick Smart produced a verified 277,923-byte result under a 300,000-byte limit after two full candidates; Discord verified the source-resolution Q100 result in one full encode. These are single-clip observations on the development machine, not general speed guarantees.
