# Changelog

## 0.1.0 — MVP

- Top-level CAD tab via `orca.pages.PagesPluginCapabilityBase` (FilamentHub-style).
- Parametric primitives: Box, Cylinder, Tube, Bracket-with-holes (sliders + validation).
- Code editor + 3 built-in examples, `result = ...` convention.
- Export STL (`export_stl`), STEP (`export_step`), 3MF (`Mesher`) to plugin-local `exports/`.
- Worker-thread exec, progress + result/error posts, audit-friendly paths.
- Script-capability fallback with upgrade message on builds lacking `orca.pages`.
