# -*- coding: utf-8 -*-
"""
墙图（Wall Graph）表示的户型生成

参考 WallPlan（ACM TOG / SIGGRAPH 2022）的核心思想：
    户型不是一张"像素图"，而是一张**图**——
    顶点是墙的交点，边是墙段，面是房间（带标签）。

WallPlan 用神经网络来"生成这张图"（WinNet / GraphNet / LabelNet，
在 RPLAN 的 8 万张户型图上训练）。本模块**不复现神经网络部分**，
而是用一个经典的**递归切分**（guillotine partition）来生成墙图，
目的是把"墙图表示"这个思想落地，并与 Power Diagram 路线做对照。

两条路线的本质差别：

    Power Diagram（上一版）      本模块（墙图 + 递归切分）
    ------------------------     ------------------------
    区域是加权 Voronoi 胞腔      区域是递归切出来的矩形
    边界随意倾斜，房间是不规则      边界全部横平竖直，房间是矩形
    多边形，边数不定               （符合真实建筑制图）
    面积需要"反解权重"逼近        面积由切分比例直接决定，天然精确
    形状不可控                     形状可控（可选切分方向）
"""
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    r'C:\Windows\Fonts\msyh.ttc', r'C:\Windows\Fonts\msyhbd.ttc',
    r'C:\Windows\Fonts\simhei.ttf',
]

ROOM_COLORS = {
    '客厅': (246, 231, 205), '餐厅': (250, 240, 214), '主卧': (208, 226, 246),
    '次卧': (219, 234, 250), '卧室': (208, 226, 246), '书房': (226, 218, 244),
    '厨房': (250, 219, 199), '卫生间': (206, 233, 233), '阳台': (216, 238, 214),
    '玄关': (232, 232, 232), '储物间': (238, 232, 222),
}
DEFAULT_COLOR = (235, 235, 235)

# 户型预设：外轮廓尺寸(米) + 房间列表 [(名称, 面积占比, 期望邻接的房间)]
PRESETS = {
    '一室一厅': dict(
        size=(9.0, 7.0),
        rooms=[('卧室', 0.28, []), ('客厅', 0.32, []), ('厨房', 0.14, ['客厅']),
               ('阳台', 0.14, ['客厅']), ('卫生间', 0.12, ['卧室'])]),
    '两室一厅': dict(
        size=(11.0, 8.5),
        rooms=[('主卧', 0.22, []), ('次卧', 0.16, []), ('客厅', 0.26, []),
               ('厨房', 0.12, ['客厅']), ('卫生间', 0.10, ['主卧']),
               ('阳台', 0.14, ['客厅'])]),
    '三室一厅': dict(
        size=(13.0, 9.0),
        rooms=[('主卧', 0.20, []), ('次卧', 0.15, []), ('书房', 0.12, []),
               ('客厅', 0.24, []), ('厨房', 0.11, ['客厅']),
               ('卫生间', 0.09, ['主卧']), ('阳台', 0.09, ['客厅'])]),
}


def font(size, bold=False):
    order = ([FONT_CANDIDATES[1]] if bold else []) + FONT_CANDIDATES
    for p in order:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()




# ============================================================
#  一、递归切分：把矩形按面积比例切成若干子矩形
# ============================================================

def _split(rect, items_a, items_b, frac, vertical):
    """按比例把 rect 切成两块"""
    x, y, w, h = rect
    if vertical:
        w1 = w * frac
        return (x, y, w1, h), (x + w1, y, w - w1, h)
    h1 = h * frac
    return (x, y, w, h1), (x, y + h1, w, h - h1)


def _aspect(rect):
    x, y, w, h = rect
    if w <= 0 or h <= 0:
        return 1e9
    return max(w / h, h / w)


def _quality(rect, items):
    """
    给一个"待继续切分的子矩形"打分。

    关键点：单房间的矩形，长宽比越接近 1 越好（住着舒服、也像真实房间）；
    多房间的矩形，还要考虑它是否方便继续切 —— 太扁的矩形切下去全是细条。
    所以用长宽比的偏离程度做惩罚，并按房间数量加权。
    """
    n = len(items)
    a = _aspect(rect)
    return (a - 1.0) * (1.0 + 0.35 * (n - 1))


def partition(rect, items, vertical_pref=None):
    """
    递归切分（guillotine partition）。

    items: [(名称, 面积占比), ...]，顺序会影响布局，先排的靠左上。

    为什么面积天然精确？
      每一步都**按两组面积之比**来切，子矩形面积严格等于该组的总需求。
      一直递归下去，每个叶子拿到的面积自然就是它的目标面积 ——
      不需要像 Power Diagram 那样迭代反解权重。

    搜索策略：
      枚举"切在第几个房间"和"横切还是竖切"，用长宽比惩罚挑最优的一组。
      这是一个贪心策略，不保证全局最优，但对户型这种规模足够。
    """
    if len(items) == 1:
        return [dict(name=items[0][0], rect=rect)]

    total = sum(a for _, a in items)
    best = None
    for k in range(1, len(items)):
        a1 = sum(a for _, a in items[:k])
        frac = a1 / total
        for vertical in (True, False):
            r1, r2 = _split(rect, items[:k], items[k:], frac, vertical)
            if min(r1[2], r1[3], r2[2], r2[3]) <= 0:
                continue
            score = _quality(r1, items[:k]) + _quality(r2, items[k:])
            # 轻微偏好竖切，避免全横切造成条带
            if vertical:
                score *= 0.97
            if best is None or score < best[0]:
                best = (score, k, vertical)

    if best is None:
        return [dict(name=it[0], rect=rect) for it in items]

    _, k, vertical = best
    a1 = sum(a for _, a in items[:k])
    r1, r2 = _split(rect, items[:k], items[k:], a1 / total, vertical)
    return partition(r1, items[:k]) + partition(r2, items[k:])


# ============================================================
#  二、墙图：从矩形集合构建图结构
# ============================================================

def adjacency(rooms):
    """
    找出哪些房间相邻、公共墙有多长。

    这就是墙图的"边"—— 两个房间共享一段墙，才可能开门。
    判据：两个矩形在某一条边上重叠，且重叠长度大于阈值。
    """
    out = []
    for i in range(len(rooms)):
        for j in range(i + 1, len(rooms)):
            xi, yi, wi, hi = rooms[i]['rect']
            xj, yj, wj, hj = rooms[j]['rect']
            ov_x = min(xi + wi, xj + wj) - max(xi, xj)
            ov_y = min(yi + hi, yj + hj) - max(yi, yj)
            # 竖直公共边：x 方向贴齐，y 方向有重叠
            if abs((xi + wi) - xj) < 1e-6 or abs((xj + wj) - xi) < 1e-6:
                if ov_y > 0.6:
                    out.append((i, j, float(ov_y), 'v'))
            # 水平公共边
            if abs((yi + hi) - yj) < 1e-6 or abs((yj + hj) - yi) < 1e-6:
                if ov_x > 0.6:
                    out.append((i, j, float(ov_x), 'h'))
    return out


def check_constraints(rooms, want):
    """
    检查"气泡图"约束：指定的房间对必须相邻。

    WallPlan 支持这类设计约束（论文里叫 bubble diagram constraints）。
    这里做一个简化版：只要求"必须相邻"，不要求具体的相邻边。
    """
    adj = {(a, b) for a, b, _, _ in adjacency(rooms)}
    bad = []
    for name, _, need in want:
        i = next(k for k, r in enumerate(rooms) if r['name'] == name)
        for other in need:
            j = next(k for k, r in enumerate(rooms) if r['name'] == other)
            if (min(i, j), max(i, j)) not in adj:
                bad.append((name, other))
    return bad


def build_wall_graph(rooms):
    """
    构建墙图：顶点 = 墙段端点，边 = 墙段，面 = 房间。

    做法是把所有矩形的边拆成"被房间端点切断"的小段，
    再按坐标去重合并 —— 合并后的每条边就是一面实际的墙。
    """
    xs = sorted({round(v, 6) for r in rooms
                 for v in (r['rect'][0], r['rect'][0] + r['rect'][2])})
    ys = sorted({round(v, 6) for r in rooms
                 for v in (r['rect'][1], r['rect'][1] + r['rect'][3])})

    edges = set()
    for r in rooms:
        x, y, w, h = r['rect']
        # 上下两条边，按所有竖坐标切段
        for yy in (y, y + h):
            cuts = [v for v in xs if x - 1e-9 <= v <= x + w + 1e-9]
            for a, b in zip(cuts, cuts[1:]):
                if b - a > 1e-6:
                    edges.add((round(a, 6), round(yy, 6), round(b, 6), round(yy, 6)))
        for xx in (x, x + w):
            cuts = [v for v in ys if y - 1e-9 <= v <= y + h + 1e-9]
            for a, b in zip(cuts, cuts[1:]):
                if b - a > 1e-6:
                    edges.add((round(xx, 6), round(a, 6), round(xx, 6), round(b, 6)))

    verts = set()
    for x1, y1, x2, y2 in edges:
        verts.add((x1, y1))
        verts.add((x2, y2))
    faces = [(r['name'], r['rect']) for r in rooms]
    return dict(vertices=sorted(verts), edges=sorted(edges), faces=faces)


# ============================================================
#  三、渲染
# ============================================================

def _external(edges, W, H, aw, ah, eps=1e-6):
    """外墙上开的窗：位于户型外轮廓上的墙段"""
    out = []
    for x1, y1, x2, y2 in edges:
        if (abs(y1) < eps and abs(y2) < eps) or \
           (abs(y1 - ah) < eps and abs(y2 - ah) < eps) or \
           (abs(x1) < eps and abs(x2) < eps) or \
           (abs(x1 - aw) < eps and abs(x2 - aw) < eps):
            out.append((x1, y1, x2, y2))
    return out


def render(rooms, size, px_per_m=78, margin=30, title=None, door_width=0.9):
    """
    画户型图。

    和 Power Diagram 那版的差别在于：
      · 墙是横平竖直的矩形边界，直接按线段画
      · 门开在**相邻房间的公共墙**上，取公共段中点
      · 窗开在**外墙**上，画成图中的双线符号
    """
    aw, ah = size
    W, H = int(round(aw * px_per_m)), int(round(ah * px_per_m))
    canvas = Image.new('RGB', (W + margin * 2, H + margin * 2 + (34 if title else 0)),
                       (255, 255, 255))
    ox, oy = margin, margin + (34 if title else 0)

    img = Image.new('RGB', (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)

    def P(x, y):
        return (ox + x * px_per_m, oy + y * px_per_m)

    # 铺底色
    for r in rooms:
        x, y, w, h = r['rect']
        d.rectangle([P(x, y), P(x + w, y + h)],
                    fill=ROOM_COLORS.get(r['name'], DEFAULT_COLOR))

    # 墙体：把所有墙段画成粗线
    wg = build_wall_graph(rooms)
    wall_px = max(3, int(0.22 * px_per_m))       # 墙厚约 22 cm
    for x1, y1, x2, y2 in wg['edges']:
        d.line([P(x1, y1), P(x2, y2)], fill=(64, 68, 74), width=wall_px)

    # 门：相邻房间公共墙的中点开一个口
    for i, j, shared, orient in adjacency(rooms):
        xi, yi, wi, hi = rooms[i]['rect']
        xj, yj, wj, hj = rooms[j]['rect']
        if orient == 'v':
            wx = xi + wi if abs((xi + wi) - xj) < 1e-6 else xj + wj
            y0 = max(yi, yj)
            cy = y0 + shared / 2.0
            half = min(door_width, shared * 0.6) / 2.0
            d.line([P(wx, cy - half), P(wx, cy + half)], fill=(255, 255, 255),
                   width=wall_px + 2)
        else:
            wy = yi + hi if abs((yi + hi) - yj) < 1e-6 else yj + hj
            x0 = max(xi, xj)
            cx = x0 + shared / 2.0
            half = min(door_width, shared * 0.6) / 2.0
            d.line([P(cx - half, wy), P(cx + half, wy)], fill=(255, 255, 255),
                   width=wall_px + 2)

    # 窗：外墙上的双线符号
    for x1, y1, x2, y2 in _external(wg['edges'], W, H, aw, ah):
        if abs(y1 - y2) < 1e-6:                  # 水平墙
            if abs(y1) < 1e-6:
                yy = 0.05
            else:
                yy = ah - 0.05
            d.line([P(x1 + 0.15, yy), P(x2 - 0.15, yy)], fill=(120, 170, 215), width=2)
            d.line([P(x1 + 0.15, yy + 0.06), P(x2 - 0.15, yy + 0.06)],
                   fill=(120, 170, 215), width=2)
        else:                                     # 垂直墙
            xx = 0.05 if abs(x1) < 1e-6 else aw - 0.05
            d.line([P(xx, y1 + 0.15), P(xx, y2 - 0.15)], fill=(120, 170, 215), width=2)
            d.line([P(xx + 0.06, y1 + 0.15), P(xx + 0.06, y2 - 0.15)],
                   fill=(120, 170, 215), width=2)

    # 外框
    d.rectangle([P(0, 0), P(aw, ah)], outline=(44, 48, 54), width=2)

    # 标注
    fn, fs = font(17, True), font(13)
    total_m2 = aw * ah
    for r in rooms:
        x, y, w, h = r['rect']
        cx, cy = P(x + w / 2, y + h / 2)
        d.text((cx, cy - 11), r['name'], font=fn, fill=(40, 42, 48), anchor='mm')
        d.text((cx, cy + 11), '%.1f m²' % (w * h), font=fs, fill=(96, 100, 108),
               anchor='mm')

    canvas.paste(img, (ox, oy))
    if title:
        ImageDraw.Draw(canvas).text((ox, oy - 30), title, font=font(19, True),
                                    fill=(30, 32, 38))
    return canvas


def _shape_penalty(rooms):
    """形状惩罚：所有房间长宽比偏离 1 的程度之和"""
    return sum(_aspect(r['rect']) - 1.0 for r in rooms)


def _score(rooms, want):
    """
    给一次划分打分。两项：
      · 违反的邻接约束数（权重很高 —— 约束是硬性设计要求）
      · 房间形状惩罚（长宽比越接近 1 越好）
    """
    bad = check_constraints(rooms, want)
    return len(bad) * 50.0 + _shape_penalty(rooms), len(bad)


def generate(preset, targets=None, tries=4000, seed=7, search=True):
    """
    按预设生成一个户型（墙图路线）。

    为什么要搜索？
      递归切分的结果**依赖于房间的排列顺序** —— 谁先切，谁就占左上。
      而不同的顺序会导致不同的邻接关系，也就可能违反"卫生间必须挨着主卧"
      这类设计约束。

      所以这里在**房间顺序**上做随机搜索：跑 tries 次随机排列，
      取"约束违反数最少、房间形状最好"的那一个。

      这其实就是 WallPlan 论文里"支持气泡图约束"的简化版 ——
      论文用神经网络学出来，这里用搜索凑出来。
    """
    import random

    cfg = PRESETS[preset]
    aw, ah = cfg['size']
    # targets 可以覆盖预设里的面积（交互程序拖动滑块时用）
    if targets:
        base = [(n, float(targets.get(n, a))) for n, a, _ in cfg['rooms']]
    else:
        base = [(n, a) for n, a, _ in cfg['rooms']]
    base = [(n, a / sum(b for _, b in base)) for n, a in base]
    want = cfg['rooms']
    rect = (0.0, 0.0, aw, ah)

    rooms = partition(rect, base)          # 原始顺序的结果，作为兜底
    best_score, best_bad = _score(rooms, want)
    used = 0

    if search:
        rng = random.Random(seed)
        idx = list(range(len(base)))
        for t in range(tries):
            if t == 0:
                order = base
            else:
                perm = idx[:]
                rng.shuffle(perm)
                order = [base[i] for i in perm]
            cand = partition(rect, order)
            s, bad = _score(cand, want)
            used = t + 1
            if s < best_score:
                best_score, best_bad, rooms = s, bad, cand
            if bad == 0 and s < 6.0:        # 约束全满足且形状可接受，提前收工
                break

    total = aw * ah
    for r in rooms:
        r['area'] = r['rect'][2] * r['rect'][3] / total
        r['m2'] = r['rect'][2] * r['rect'][3]

    return dict(rooms=rooms, size=cfg['size'], total_m2=total, preset=preset,
                targets={n: a for n, a in base},     # 实际使用的目标，不是预设值
                wants=cfg['rooms'], violations=best_bad, tries_used=used,
                shape=_shape_penalty(rooms))
