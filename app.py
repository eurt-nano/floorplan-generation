# -*- coding: utf-8 -*-
"""
户型自动生成 · 交互版

拖动滑块调整各房间的面积配比，户型图实时重算。
可在两条技术路线之间切换：

    墙图 + 递归切分   约 0.01 秒，可做到拖动即刷新
    Power Diagram    约 1.2 秒，拖动时用降配参数、松手后出全质量图

界面用 Tkinter（Python 自带，不需安装任何东西）。

用法：
    python app.py
"""
import os
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

import floorplan as PD
import wallgraph as WG

HERE = os.path.dirname(os.path.abspath(__file__))
UI_FONT = ('Microsoft YaHei', 9)
UI_FONT_B = ('Microsoft YaHei', 9, 'bold')

# 注意：Windows 高 DPI 下 Tk 有约 1.33 的缩放系数，
# 这里给的是**逻辑像素**，实际显示会被放大，所以要按屏幕留足余量。
CANVAS_W, CANVAS_H = 620, 500


class App:
    def __init__(self, root):
        self.root = root
        root.title('户型自动生成 · 交互版')
        root.configure(bg='#f2f3f5')

        self.route = tk.StringVar(value='wall')      # wall | power
        self.preset = tk.StringVar(value='两室一厅')
        self.sliders = {}
        self.weights = {}
        self.targets = {}
        self.photo = None                            # 必须持有引用，否则被回收
        self.pending = None                          # 防抖用的定时器 id
        self.last_image = None
        self.busy = False

        self._build_ui()
        self._load_preset('两室一厅')
        self.render()

    # ------------------------------------------------------------ 界面

    def _build_ui(self):
        # 右侧面板必须**先** pack。
        # Tk 的 packer 按调用顺序分配空间：左边带 expand=True，
        # 如果先 pack 左边，它会把空间占满，右边固定宽度的面板就被挤出窗口了。
        right = tk.Frame(self.root, bg='white', width=310)
        right.pack(side='right', fill='y', padx=(0, 10), pady=10)
        right.pack_propagate(False)

        left = tk.Frame(self.root, bg='#f2f3f5')
        left.pack(side='left', fill='both', expand=True, padx=(10, 6), pady=10)

        self.canvas = tk.Canvas(left, width=CANVAS_W, height=CANVAS_H,
                                bg='white', highlightthickness=1,
                                highlightbackground='#d0d3d8')
        self.canvas.pack()

        self.status = tk.Label(left, text='', font=UI_FONT, bg='#f2f3f5',
                               fg='#444', anchor='w')
        self.status.pack(fill='x', pady=(6, 0))

        # ---- 生成方法 ----
        tk.Label(right, text='生成方法', font=UI_FONT_B, bg='white',
                 anchor='w').pack(fill='x', padx=14, pady=(14, 4))
        for val, txt, note in (
                ('wall', '墙图 + 递归切分', '约 0.01 秒 · 矩形房间 · 支持邻接约束'),
                ('power', 'Power Diagram', '约 1.2 秒 · 多边形房间 · 权重反解')):
            f = tk.Frame(right, bg='white')
            f.pack(fill='x', padx=14)
            tk.Radiobutton(f, text=txt, variable=self.route, value=val,
                           font=UI_FONT, bg='white', activebackground='white',
                           command=self.on_route).pack(anchor='w')
            tk.Label(f, text=note, font=('Microsoft YaHei', 8), bg='white',
                     fg='#8a8e95', anchor='w').pack(fill='x', padx=(20, 0))

        # ---- 户型预设 ----
        tk.Label(right, text='户型预设', font=UI_FONT_B, bg='white',
                 anchor='w').pack(fill='x', padx=14, pady=(14, 4))
        cb = ttk.Combobox(right, textvariable=self.preset, state='readonly',
                          values=list(WG.PRESETS.keys()), font=UI_FONT)
        cb.pack(fill='x', padx=14)
        cb.bind('<<ComboboxSelected>>',
                lambda e: self._load_preset(self.preset.get()))

        # ---- 房间面积 ----
        tk.Label(right, text='房间面积配比（拖动调整）', font=UI_FONT_B, bg='white',
                 anchor='w').pack(fill='x', padx=14, pady=(16, 2))
        tk.Label(right, text='拖动任一滑块，其余房间会按比例自动调整',
                 font=('Microsoft YaHei', 8), bg='white', fg='#8a8e95',
                 anchor='w').pack(fill='x', padx=14)

        self.rooms_box = tk.Frame(right, bg='white')
        self.rooms_box.pack(fill='x', padx=14, pady=(6, 0))

        # ---- 指标 ----
        self.metrics = tk.Label(right, text='', font=('Consolas', 9), bg='#f7f8fa',
                                fg='#24282e', justify='left', anchor='w',
                                relief='solid', bd=1, padx=10, pady=8)
        self.metrics.pack(fill='x', padx=14, pady=(16, 0))

        # ---- 按钮 ----
        btns = tk.Frame(right, bg='white')
        btns.pack(fill='x', padx=14, pady=14)
        tk.Button(btns, text='重置', font=UI_FONT, command=self.reset,
                  width=8).pack(side='left')
        tk.Button(btns, text='导出图片', font=UI_FONT, command=self.export,
                  width=10).pack(side='right')

        tk.Label(right, text='提示：调整面积后，若某房间变得过小或过大，\n'
                             '形状惩罚会上升，房间会变细长。',
                 font=('Microsoft YaHei', 8), bg='white', fg='#8a8e95',
                 justify='left', anchor='w').pack(fill='x', padx=14, pady=(0, 14))

    def _load_preset(self, name):
        """切换户型：按预设重建滑块"""
        for w in self.rooms_box.winfo_children():
            w.destroy()
        self.sliders.clear()
        self.weights.clear()

        cfg = WG.PRESETS[name]
        self.targets = {n: a for n, a, _ in cfg['rooms']}
        for n, a, _ in cfg['rooms']:
            self.weights[n] = int(round(a * 100))
            row = tk.Frame(self.rooms_box, bg='white')
            row.pack(fill='x', pady=1)
            tk.Label(row, text=n, font=UI_FONT, bg='white', width=5,
                     anchor='w').pack(side='left')
            s = tk.Scale(row, from_=1, to=60, orient='horizontal',
                         showvalue=0, length=190, bg='white',
                         highlightthickness=0, sliderlength=14,
                         command=lambda v, k=n: self.on_slider(k, v))
            s.set(self.weights[n])
            s.pack(side='left', padx=(4, 6))
            lab = tk.Label(row, text='', font=('Consolas', 9), bg='white',
                           width=13, anchor='e')
            lab.pack(side='right')
            self.sliders[n] = (s, lab)

    # ------------------------------------------------------------ 交互

    def on_route(self):
        self.render()

    def on_slider(self, name, value):
        self.weights[name] = float(value)
        self._refresh_labels()
        # 墙图路线够快，直接重算；Power Diagram 走防抖，避免拖动时卡顿
        if self.route.get() == 'wall':
            self.render()
        else:
            self.schedule_render(220)

    def schedule_render(self, delay):
        if self.pending:
            self.root.after_cancel(self.pending)
        self.pending = self.root.after(delay, self.render)

    def _refresh_labels(self):
        total = sum(self.weights.values()) or 1.0
        for n, (_, lab) in self.sliders.items():
            pct = self.weights[n] / total * 100
            self.targets[n] = self.weights[n] / total
            lab.config(text='%5.1f%%' % pct)

    def reset(self):
        self._load_preset(self.preset.get())
        self.render()

    # ------------------------------------------------------------ 生成

    def render(self):
        if self.busy:
            return
        self.busy = True
        self.pending = None
        self._refresh_labels()
        fast = self.route.get() == 'power'
        try:
            t0 = time.time()
            if self.route.get() == 'wall':
                img, err, extra = self._gen_wall()
            else:
                img, err, extra = self._gen_power(fast)
            dt = time.time() - t0
            self.last_image = img
            self._show(img)
            self._show_metrics(err, dt, extra)
        except Exception as e:
            self.status.config(text='生成失败：%s' % e)
        finally:
            self.busy = False
            # 若拖动中又排了新的任务，等它触发；否则补一次全质量
            if self.route.get() == 'power' and fast and not self.pending:
                self.schedule_render(260)

    def _gen_wall(self):
        fp = WG.generate(self.preset.get(), targets=self.targets)
        img = WG.render(fp['rooms'], fp['size'])
        err = sum(abs(r['area'] - fp['targets'][r['name']]) for r in fp['rooms'])
        extra = dict(kind='wall', tries=fp['tries_used'],
                     bad=len(WG.check_constraints(fp['rooms'], fp['wants'])),
                     verts=len(WG.build_wall_graph(fp['rooms'])['vertices']))
        return img, err, extra

    def _gen_power(self, fast):
        if fast:      # 拖动中：降分辨率、减迭代
            W, iters, ow = 620, 90, 150
        else:         # 松手后：出全质量图
            W, iters, ow = 860, 300, 220
        fp = PD.generate(self.preset.get(), targets=self.targets, W=W,
                         iters=iters, lr=0.4, opt_w=ow)
        img = PD.render(fp)
        err = sum(abs(r['target'] - r['actual']) for r in fp['rooms'])
        extra = dict(kind='power', fast=fast, reps=0)
        return img, err, extra

    # ------------------------------------------------------------ 显示

    def _show(self, img):
        w, h = img.size
        s = min(CANVAS_W / w, CANVAS_H / h, 1.0)
        if s < 1.0:
            img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(img)
        self.canvas.delete('all')
        self.canvas.create_image(CANVAS_W // 2, CANVAS_H // 2,
                                 image=self.photo, anchor='center')

    def _show_metrics(self, err, dt, extra):
        if extra['kind'] == 'wall':
            txt = ('路线  墙图 + 递归切分\n'
                   '面积误差  %.1e   （按比例切分，天然精确）\n'
                   '邻接约束  %s\n'
                   '搜索次数  %d\n'
                   '墙图规模  %d 个顶点\n'
                   '耗时      %.3f 秒'
                   % (err, '全部满足' if extra['bad'] == 0 else '违反 %d 项' % extra['bad'],
                      extra['tries'], extra['verts'], dt))
        else:
            tag = '（拖动中·降配预览）' if extra.get('fast') else '（全质量）'
            txt = ('路线  Power Diagram %s\n'
                   '面积误差  %.1e\n'
                   '说明  迭代反解权重，误差随迭代收敛\n'
                   '耗时      %.3f 秒' % (tag, err, dt))
        self.metrics.config(text=txt)
        self.status.config(text='拖动滑块调整面积配比　|　'
                                '当前：%s · %s' % (self.preset.get(),
                                                   '墙图' if extra['kind'] == 'wall'
                                                   else 'Power Diagram'))

    # ------------------------------------------------------------ 导出

    def export(self):
        if self.last_image is None:
            return
        p = filedialog.asksaveasfilename(
            defaultextension='.png', initialfile='%s_户型.png' % self.preset.get(),
            filetypes=[('PNG 图片', '*.png')])
        if p:
            self.last_image.save(p)
            messagebox.showinfo('导出成功', '已保存到：\n%s' % p)


def main():
    root = tk.Tk()
    # 先定窗口大小，再建控件 —— 否则控件会按默认尺寸布局，右侧容易被切掉
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    w = min(1300, max(1000, int(sw * 0.86)))
    h = min(780, max(640, int(sh * 0.88)))
    root.geometry('%dx%d+%d+%d' % (w, h, (sw - w) // 2, max(0, (sh - h) // 2 - 20)))
    root.minsize(980, 620)
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
