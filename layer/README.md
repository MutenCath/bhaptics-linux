# bhaptics OpenXR implicit API layer (PoC)

**STATUS: EXPERIMENTAL.** Proof of concept, not a shipped feature of
bhaptics-linux. Single C file, no dependencies beyond gcc and the vendored
Khronos headers.

## What it is

An OpenXR **implicit API layer** (`libXrApiLayer_bhaptics.so`). Once
installed, the OpenXR loader loads it into **every** OpenXR application —
including Windows VR games under Proton, because Wine's `wineopenxr.so`
loads the *native* Linux OpenXR loader, and native implicit layers apply to
it just as they do to native games.

The layer intercepts `xrApplyHapticFeedback` and `xrStopHapticFeedback` and
mirrors each vibration to the bhaptics-linux daemon as an OSC 1.0 packet
over UDP to `127.0.0.1:9001` (the daemon's existing OSC bridge port):

```
/vest/tap  <side: "left"|"right"|"both">  <amp: float 0..1>  <ms: int32>
```

- The side comes from the `subactionPath` (resolved via `xrPathToString`;
  paths containing `left`/`right` map to those sides, anything else —
  including `XR_NULL_PATH` — is `both`).
- Amplitude is clamped to `[0, 1]`; a negative amplitude (sentinel
  convention) becomes `0.5`.
- `XrDuration` (int64 nanoseconds) is converted to milliseconds;
  unbounded duration (`0`, the 1.1 semantics, and 1.0's
  `XR_UNBOUNDED_DURATION` too) is capped at 2000 ms.

The send is **best-effort and never blocks**: a non-blocking UDP socket, a
failed `sendto` only logs, and the layer always calls through to the next
layer / loader and returns its result unchanged. A broken daemon can never
affect the game.

## Build & test

```sh
make          # builds libXrApiLayer_bhaptics.so (gcc only, -Wall -Wextra -Werror)
make test     # builds and runs the mock-loader harness (needs a free UDP port 9001)
```

## Install (all under your home directory)

```sh
mkdir -p ~/.local/share/openxr/1/api_layers/implicit.d
cp libXrApiLayer_bhaptics.so ~/.local/share/openxr/1/api_layers/implicit.d/
cp manifest.json ~/.local/share/openxr/1/api_layers/implicit.d/XR_APILAYER_NOVENDOR_bhaptics_capture.json
```

The manifest's `library_path` (`./libXrApiLayer_bhaptics.so`) is resolved
relative to the manifest file itself, so keeping both in `implicit.d/`
works with no path editing. Manifest field meanings: `file_format_version
"1.0.0"`, `api_layer.name` (must match the layer's negotiation name),
`library_path`, `api_version "1.0"` (ignored by current loaders),
`implementation_version "1"`, `description`,
`disable_environment "DISABLE_BHAPTICS_XR_LAYER"`.

## Disabling

The loader respects the manifest's `disable_environment`: launching an
application with `DISABLE_BHAPTICS_XR_LAYER=1` (any value) makes the loader
skip this layer entirely.

## Debug logging

Set `BHAPTICS_XR_LOG=1` to enable stderr logging (negotiation, per-haptic
lines, socket errors). Off by default — the layer is silent otherwise.

## Design notes / limitations

- Negotiation implements `xrNegotiateLoaderApiLayerInterface` per the
  vendored OpenXR 1.1.43 headers (3-argument form), validates struct and
  interface versions, and reports `layerInterfaceVersion = 1`.
- `xrCreateApiLayerInstance` advances the `nextInfo` chain
  (`nextInfo = apiLayerInfo->nextInfo->next`) before calling down — the
  classic layer bug, done correctly.
- Per-instance state lives in a fixed 4-slot table (no pthreads by design;
  games use one instance). Hooks are per-slot function copies because
  `xrApplyHapticFeedback` receives only a session, not an instance. Slots are
  never freed (`xrDestroyInstance` isn't hooked), so after 4 create/destroy
  cycles in one process capture silently disables — irrelevant for real
  games, worth knowing for synthetic tests.
- The UDP socket is shared, opened lazily, and never closed (process
  lifetime). A two-thread race at first use can leak one fd — benign.
- `xrStopHapticFeedback` is intercepted but sends nothing: the daemon
  contract defines only `/vest/tap`, and this PoC invents no new endpoints.
- No audio, no config file, no instance tracking beyond the dispatch slots.
