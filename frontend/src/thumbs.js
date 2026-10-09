// Model preview thumbnails, rendered by `xvfb-run -a python3 dev/thumbs.py`.
// Vite inlines every asset into the single-file page (assetsInlineLimit), so a
// thumbnail travels inside orcad.py like the rest of the page.
const modules = import.meta.glob('./thumbs/*.png', { eager: true, import: 'default' })

export const THUMBNAILS = Object.fromEntries(Object.entries(modules).map(
  ([path, url]) => [path.slice('./thumbs/'.length, -'.png'.length), url]))
