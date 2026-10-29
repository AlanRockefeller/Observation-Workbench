# Identify window loses Omarchy tiled-fullscreen state

Status: documented for later investigation; no application or Hyprland
configuration change has been made.

## Symptom

In the Identify window, `Ctrl+Super+F` initially makes the window appear
fullscreen. Pressing an Identify shortcut such as `A` (Agree) or `R`
(Reviewed) can then make the window return to its tiled size.

## Environment observed

- Hyprland 0.56.2
- Omarchy's stock `Ctrl+Super+F` binding, labelled **Tiled full screen**
- Native Wayland PySide6/Qt application (`class: python3`, title:
  `iNaturalist Identify`)
- `hyprctl configerrors` reported no configuration errors

## Current diagnosis

The Identify shortcuts do not resize the window. `A` and `R` only start their
respective application actions, and the Identify window contains no
`showNormal()`, `showFullScreen()`, or equivalent action-specific window-state
change.

The stock Omarchy binding runs
`omarchy-hyprland-window-tiled-fullscreen-toggle`. At the time this was
investigated, that command toggled Hyprland's fullscreen state between:

```text
internal=0, client=2
internal=0, client=0
```

Hyprland documents `internal=0, client=2` as leaving the window in the normal
compositor layout while reporting fullscreen state to the application. This
client-only state is intentionally different from compositor-owned fullscreen.
The leading hypothesis is that Qt later requests or acknowledges a normal
client state during an interaction, which clears the client-only fullscreen
state and exposes the original tiled geometry. This is consistent with the
symptom, but an event-level trace has not yet been captured.

Reference: [Hyprland fullscreen-state documentation](https://wiki.hypr.land/Configuring/Basics/Dispatchers/#fullscreenstate)

## Current workarounds

- `Super+F`: regular fullscreen
- `Super+Alt+F`: maximized/full-width mode

These use compositor-managed geometry and should be less susceptible to a Qt
client-state change.

## Options to revisit

1. Rebind `Ctrl+Super+F` to a compositor-owned state, such as internal
   fullscreen with the client kept normal (`internal=2, client=0`), or an
   internal maximized state. Recheck the current Hyprland dispatcher syntax
   before making this change because fullscreen-state handling changes between
   Hyprland releases.
2. Add an application-owned fullscreen shortcut to Identify if consistent
   cross-desktop behavior is preferable to a Hyprland-specific override.
3. Confirm the hypothesis by recording `fullscreen` and `fullscreenClient`
   from `hyprctl activewindow -j` immediately before `Ctrl+Super+F`, after it,
   and after pressing `A` or `R`.

If a Hyprland override is chosen later, put it in
`~/.config/hypr/bindings.lua`; do not edit the packaged files under
`/usr/share/omarchy/`.
