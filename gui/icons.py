"""Crisp, dependency-free media-control icons drawn with QPainter.

Emoji glyphs render inconsistently across machines and look out of place in a styled
player. These vector icons are painted at request time (cached per key), so the player
gets a clean, consistent look with no icon-font or SVG-asset dependency.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

_CACHE: dict[tuple, QIcon] = {}


def icon(kind: str, color: str = "#e8e9f0", size: int = 22) -> QIcon:
    """Return a cached QIcon for ``kind`` drawn in ``color`` at ``size`` logical px."""
    key = (kind, color, size)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    dpr = 2
    pm = QPixmap(size * dpr, size * dpr)
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    _draw(p, kind, QColor(color), float(size))
    p.end()
    result = QIcon(pm)
    _CACHE[key] = result
    return result


def _tri(p: QPainter, pts) -> None:
    p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in pts]))


def _draw(p: QPainter, kind: str, c: QColor, s: float) -> None:
    pad = s * 0.16
    mid = s / 2
    p.setPen(Qt.NoPen)
    p.setBrush(c)

    if kind == "play":
        _tri(p, [(s * 0.24, pad), (s * 0.24, s - pad), (s - pad, mid)])
    elif kind == "pause":
        w = s * 0.15
        r = s * 0.06
        p.drawRoundedRect(QRectF(s * 0.30, pad, w, s - 2 * pad), r, r)
        p.drawRoundedRect(QRectF(s * 0.55, pad, w, s - 2 * pad), r, r)
    elif kind == "prev":
        bar = s * 0.13
        p.drawRoundedRect(QRectF(pad, pad, bar, s - 2 * pad), 1.5, 1.5)
        _tri(p, [(s - pad, pad), (s - pad, s - pad), (pad + bar + s * 0.04, mid)])
    elif kind == "next":
        bar = s * 0.13
        p.drawRoundedRect(QRectF(s - pad - bar, pad, bar, s - 2 * pad), 1.5, 1.5)
        _tri(p, [(pad, pad), (pad, s - pad), (s - pad - bar - s * 0.04, mid)])
    elif kind == "skip":  # skip-forward: triangle + bar (for "salta intro"/next)
        bar = s * 0.12
        _tri(p, [(pad, pad), (pad, s - pad), (s * 0.62, mid)])
        p.drawRoundedRect(QRectF(s * 0.66, pad, bar, s - 2 * pad), 1.5, 1.5)
    elif kind == "volume":
        path = QPainterPath()
        path.moveTo(pad, s * 0.38)
        path.lineTo(s * 0.30, s * 0.38)
        path.lineTo(s * 0.50, pad)
        path.lineTo(s * 0.50, s - pad)
        path.lineTo(s * 0.30, s * 0.62)
        path.lineTo(pad, s * 0.62)
        path.closeSubpath()
        p.drawPath(path)
        pen = QPen(c, s * 0.09)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(s * 0.44, s * 0.30, s * 0.34, s * 0.40), -55 * 16, 110 * 16)
        p.drawArc(QRectF(s * 0.44, s * 0.20, s * 0.52, s * 0.60), -50 * 16, 100 * 16)
    elif kind in ("fullscreen", "fullscreen_exit"):
        pen = QPen(c, s * 0.10)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        a, b = pad, s - pad
        d = s * 0.20
        if kind == "fullscreen":
            corners = [
                [(a, a + d), (a, a), (a + d, a)],
                [(b - d, a), (b, a), (b, a + d)],
                [(a, b - d), (a, b), (a + d, b)],
                [(b - d, b), (b, b), (b, b - d)],
            ]
        else:
            corners = [
                [(a, a + d), (a + d, a + d), (a + d, a)],
                [(b - d, a), (b - d, a + d), (b, a + d)],
                [(a, b - d), (a + d, b - d), (a + d, b)],
                [(b - d, b), (b - d, b - d), (b, b - d)],
            ]
        for c3 in corners:
            path = QPainterPath()
            path.moveTo(*c3[0])
            path.lineTo(*c3[1])
            path.lineTo(*c3[2])
            p.drawPath(path)
    elif kind == "download":
        pen = QPen(c, s * 0.10)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawLine(QPointF(mid, pad), QPointF(mid, s * 0.62))
        path = QPainterPath()
        path.moveTo(s * 0.32, s * 0.44)
        path.lineTo(mid, s * 0.66)
        path.lineTo(s * 0.68, s * 0.44)
        p.drawPath(path)
        p.drawLine(QPointF(pad, s - pad), QPointF(s - pad, s - pad))
    elif kind == "external":
        pen = QPen(c, s * 0.10)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        path = QPainterPath()
        path.moveTo(pad, s * 0.34)
        path.lineTo(pad, s - pad)
        path.lineTo(s * 0.66, s - pad)
        p.drawPath(path)
        p.drawLine(QPointF(s * 0.46, s * 0.54), QPointF(s - pad, pad))
        arrow = QPainterPath()
        arrow.moveTo(s * 0.60, pad)
        arrow.lineTo(s - pad, pad)
        arrow.lineTo(s - pad, s * 0.40)
        p.drawPath(arrow)
