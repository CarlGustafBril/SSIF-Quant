"""Minimal plots written as SVG files, so no plotting library is needed. Open them in any browser."""
import numpy as np

W, H, PAD_L, PAD_R, PAD_T, PAD_B = 460, 400, 62, 18, 40, 48


def _ticks(lo, hi, n=5):
    step = 10 ** np.floor(np.log10((hi - lo) / n))
    for mult in (1, 2, 5, 10):
        if (hi - lo) / (step * mult) <= n:
            step *= mult
            break
    ticks = np.arange(np.ceil(lo / step) * step, hi + step / 2, step)
    return ticks[(ticks >= lo) & (ticks <= hi)]


def _panel(p, left):
    """One scatter panel. p: title, xlabel, ylabel, x, y, and optionally diagonal=True, connect=True."""
    x, y = np.asarray(p["x"], dtype=float), np.asarray(p["y"], dtype=float)
    keep = np.isfinite(x) & np.isfinite(y)
    x, y = x[keep], y[keep]
    lo_x, hi_x = (min(x.min(), y.min()), max(x.max(), y.max())) if p.get("diagonal") else (x.min(), x.max())
    lo_y, hi_y = (lo_x, hi_x) if p.get("diagonal") else (y.min(), y.max())
    inner_w, inner_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B

    def sx(v):
        return left + PAD_L + (v - lo_x) / (hi_x - lo_x) * inner_w

    def sy(v):
        return PAD_T + inner_h - (v - lo_y) / (hi_y - lo_y) * inner_h

    out = [f'<rect x="{left + PAD_L}" y="{PAD_T}" width="{inner_w}" height="{inner_h}" fill="white" stroke="#888"/>',
           f'<text x="{left + PAD_L + inner_w / 2}" y="22" text-anchor="middle" font-size="14">{p["title"]}</text>',
           f'<text x="{left + PAD_L + inner_w / 2}" y="{H - 10}" text-anchor="middle" font-size="12">{p["xlabel"]}</text>',
           f'<text x="{left + 14}" y="{PAD_T + inner_h / 2}" text-anchor="middle" font-size="12" '
           f'transform="rotate(-90 {left + 14} {PAD_T + inner_h / 2})">{p["ylabel"]}</text>']
    for t in _ticks(lo_x, hi_x):
        out.append(f'<line x1="{sx(t):.1f}" y1="{PAD_T}" x2="{sx(t):.1f}" y2="{PAD_T + inner_h}" stroke="#eee"/>')
        out.append(f'<text x="{sx(t):.1f}" y="{PAD_T + inner_h + 15}" text-anchor="middle" font-size="11">{t:g}</text>')
    for t in _ticks(lo_y, hi_y):
        out.append(f'<line x1="{left + PAD_L}" y1="{sy(t):.1f}" x2="{left + PAD_L + inner_w}" y2="{sy(t):.1f}" stroke="#eee"/>')
        out.append(f'<text x="{left + PAD_L - 6}" y="{sy(t) + 4:.1f}" text-anchor="end" font-size="11">{t:g}</text>')
    if p.get("diagonal"):
        out.append(f'<line x1="{sx(lo_x):.1f}" y1="{sy(lo_x):.1f}" x2="{sx(hi_x):.1f}" y2="{sy(hi_x):.1f}" stroke="black"/>')
    if p.get("connect"):
        points = " ".join(f"{sx(a):.1f},{sy(b):.1f}" for a, b in zip(x, y))
        out.append(f'<polyline points="{points}" fill="none" stroke="#1f77b4" stroke-width="1.5"/>')
    radius = 3.5 if len(x) < 50 else 1.6
    out += [f'<circle cx="{sx(a):.1f}" cy="{sy(b):.1f}" r="{radius}" fill="#1f77b4"/>' for a, b in zip(x, y)]
    return out


def save(path, panels):
    """Write the panels side by side to an SVG file."""
    body = []
    for n, p in enumerate(panels):
        body += _panel(p, n * W)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W * len(panels)}" height="{H}" '
                f'font-family="sans-serif">\n<rect width="100%" height="100%" fill="white"/>\n' + "\n".join(body) + "\n</svg>\n")


def thin(frame, n=1500):
    """Evenly spaced rows of a sorted table plus its first and last 200, so tails stay fully visible."""
    if len(frame) <= n:
        return frame
    keep = np.unique(np.concatenate([np.arange(200), np.arange(len(frame) - 200, len(frame)),
                                     np.linspace(0, len(frame) - 1, n).astype(int)]))
    return frame.iloc[keep]
