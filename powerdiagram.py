# -*- coding: utf-8 -*-
"""
Power Diagram（功率图）核心算法

Power Diagram 是 Voronoi 图的加权推广：
    Voronoi：每个像素归给"距离最近"的站点
    Power  ：每个像素归给"功率距离最小"的站点，功率距离 = 距离² - 权重

多出来的权重 w 让每个区域可以独立"膨胀"或"收缩"：
    w 增大 -> 该区域变大
    w 减小 -> 该区域变小

这正是户型自动生成的关键 —— 给定每个房间的目标面积，
反解出一组权重，让各区域面积恰好匹配。

坐标统一归一化到 [0, 1]，这样权重的量级与坐标可比，调参更稳定。
"""
import numpy as np


def grid(W, H):
    """生成归一化坐标网格，返回 (X, Y)，各为 (H, W)"""
    xs = (np.arange(W) + 0.5) / W
    ys = (np.arange(H) + 0.5) / H
    return np.meshgrid(xs, ys)


def power_labels(sites, weights, W, H):
    """
    计算 Power Diagram 的归属标签。

    参数
        sites   : (N, 2) 站点坐标，取值 [0,1]
        weights : (N,)   权重
        W, H    : 输出分辨率

    返回
        (H, W) 的 int 标签图，每个像素是所属站点的编号

    做法就是暴力枚举：对每个像素，比较它到所有站点的功率距离，取最小。
    复杂度 O(W*H*N)。对 512×512、N=6 来说完全够用，
    而且用 numpy 向量化之后每个站点一次矩阵运算，实测毫秒级。
    """
    X, Y = grid(W, H)
    best = np.full((H, W), np.inf)
    lab = np.zeros((H, W), np.int32)
    for i in range(len(sites)):
        sx, sy = sites[i]
        d = (X - sx) ** 2 + (Y - sy) ** 2 - weights[i]
        m = d < best
        best[m] = d[m]
        lab[m] = i
    return lab


def cell_areas(labels, n):
    """各区域的面积占比（按像素计数）"""
    c = np.bincount(labels.ravel(), minlength=n)
    return (c / labels.size).astype(np.float64)


def optimize_weights(sites, targets, W, H, iters=300, lr=1.6, record=False):
    """
    反解权重，使各区域面积匹配目标。

    这是一个反馈控制过程：
        1. 按当前权重算一遍 Power Diagram，量出各区域实际面积
        2. 误差 = 目标面积 - 实际面积
        3. 面积偏小就加大权重（区域膨胀），偏大就减小
        4. 重复

    两个关键细节：
      · 学习率随迭代衰减 —— 前期快速逼近，后期细调，避免在最优解附近来回震荡
      · 每步把权重减去均值 —— 权重整体平移不影响划分结果，
        但不归一化会一路漂到很大，数值上不稳定
    """
    sites = np.asarray(sites, dtype=np.float64)
    targets = np.asarray(targets, dtype=np.float64)
    targets = targets / targets.sum()
    n = len(sites)

    w = np.zeros(n)
    hist = []
    for it in range(iters):
        lab = power_labels(sites, w, W, H)
        a = cell_areas(lab, n)
        err = targets - a
        if record:
            hist.append(float(np.abs(err).sum()))
        step = lr * (1.0 - it / float(iters))     # 衰减学习率
        w = w + step * err
        w = w - w.mean()                          # 零均值化，防止整体漂移

    lab = power_labels(sites, w, W, H)
    a = cell_areas(lab, n)
    if record:
        hist.append(float(np.abs(targets - a).sum()))
    return w, lab, (hist if record else None)
