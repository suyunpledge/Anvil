"""铁砧 IDE 应用图标：深色圆角底 + 金属铁砧 + 锻造火花。1024 绘制后缩放输出多尺寸 ICO 与预览 PNG。

用法（需要 Pillow）：python packaging/make-icon.py resources/icon.ico icon-preview.png
界面里的矢量版标志在 src/renderer/src/icons.ts 的 brandMark()，改造型时两处一起改。
"""
import sys
from PIL import Image, ImageDraw, ImageFilter, ImageChops

S = 1024


def vgrad(w, h, top, bottom):
    g = Image.new("RGBA", (w, h))
    px = g.load()
    for y in range(h):
        t = y / (h - 1)
        c = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(4))
        for x in range(w):
            px[x, y] = c
    return g


def radial(w, h, cx, cy, r, color):
    """中心不透明、向外渐隐的径向光晕。"""
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    steps = 60
    for i in range(steps, 0, -1):
        rr = r * i / steps
        a = int(255 * (1 - i / steps) ** 1.6)
        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=a)
    layer.paste(Image.new("RGBA", (w, h), color), (0, 0), mask)
    return layer


def shape_mask(draw_fn):
    m = Image.new("L", (S, S), 0)
    draw_fn(ImageDraw.Draw(m))
    return m


# —— 底板：圆角方块，靛蓝到深夜色渐变 ——
base = Image.new("RGBA", (S, S), (0, 0, 0, 0))
plate_mask = shape_mask(lambda d: d.rounded_rectangle([40, 40, S - 40, S - 40], radius=220, fill=255))
plate = vgrad(S, S, (44, 52, 82, 255), (14, 17, 28, 255))
base.paste(plate, (0, 0), plate_mask)

# 锻造炉的暖光（铁砧后方）
glow = radial(S, S, 512, 470, 430, (255, 128, 48, 150))
base = Image.alpha_composite(base, Image.composite(glow, Image.new("RGBA", (S, S)), plate_mask))

# 顶部细高光与描边
rim = shape_mask(lambda d: d.rounded_rectangle([40, 40, S - 40, S - 40], radius=220, outline=255, width=4))
base.paste(Image.new("RGBA", (S, S), (130, 150, 210, 55)), (0, 0), rim)

# —— 铁砧剪影 ——
def anvil(d):
    # 砧面 + 左侧砧角 + 右侧砧踵
    d.polygon([(338, 402), (782, 402), (812, 410), (812, 456), (772, 486), (338, 486),
               (262, 470), (180, 432), (178, 420), (262, 408)], fill=255)
    # 腰部
    d.polygon([(440, 486), (676, 486), (626, 590), (490, 590)], fill=255)
    # 底座
    d.rounded_rectangle([392, 586, 724, 640], radius=14, fill=255)
    d.polygon([(372, 632), (744, 632), (774, 700), (342, 700)], fill=255)
    d.rounded_rectangle([336, 690, 780, 724], radius=16, fill=255)

anvil_mask = shape_mask(anvil)
metal = vgrad(S, S, (246, 248, 252, 255), (134, 144, 166, 255))
# 投影
shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
shadow.paste(Image.new("RGBA", (S, S), (0, 0, 0, 150)), (0, 18), anvil_mask)
shadow = shadow.filter(ImageFilter.GaussianBlur(18))
base = Image.alpha_composite(base, shadow)
anv = Image.new("RGBA", (S, S), (0, 0, 0, 0))
anv.paste(metal, (0, 0), anvil_mask)
base = Image.alpha_composite(base, anv)
# 砧面高光带
face = shape_mask(lambda d: d.polygon([(338, 402), (782, 402), (806, 408), (338, 420), (262, 412)], fill=255))
base.paste(Image.new("RGBA", (S, S), (255, 255, 255, 230)), (0, 0), ImageChops.multiply(face, anvil_mask))
# 腰部暗面，增加体积感
waist_shade = shape_mask(lambda d: d.polygon([(600, 486), (676, 486), (626, 590), (580, 590)], fill=255))
base.paste(Image.new("RGBA", (S, S), (40, 46, 66, 90)), (0, 0), waist_shade)

# —— 火花：四角星 + 光晕 + 两颗小火星 ——
def star(d, cx, cy, r_long, r_short):
    pts = []
    import math
    for i in range(8):
        ang = math.pi / 2 * (i // 2) + (math.pi / 4 if i % 2 else 0) - math.pi / 2
        r = r_long if i % 2 == 0 else r_short
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    d.polygon(pts, fill=255)

spark_glow = radial(S, S, 600, 286, 190, (255, 170, 60, 200))
base = Image.alpha_composite(base, spark_glow)
spark_mask = shape_mask(lambda d: (star(d, 600, 286, 118, 26),
                                   d.ellipse([738, 222, 770, 254], fill=255),
                                   d.ellipse([460, 214, 484, 238], fill=255),
                                   d.ellipse([700, 340, 718, 358], fill=255)))
spark = vgrad(S, S, (255, 236, 150, 255), (255, 120, 40, 255))
sp = Image.new("RGBA", (S, S), (0, 0, 0, 0))
sp.paste(spark, (0, 0), spark_mask)
base = Image.alpha_composite(base, sp)
core = shape_mask(lambda d: star(d, 600, 286, 46, 12))
base.paste(Image.new("RGBA", (S, S), (255, 252, 235, 255)), (0, 0), core)

out_ico, out_png = sys.argv[1], sys.argv[2]
base.save(out_png)
sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)]
base.resize((256, 256), Image.LANCZOS).save(out_ico, sizes=sizes)
# 小尺寸预览，检查 16/32 是否还认得出
strip = Image.new("RGBA", (16 + 32 + 48 + 64 + 40, 64), (30, 30, 30, 255))
x = 0
for s in (16, 32, 48, 64):
    strip.paste(base.resize((s, s), Image.LANCZOS), (x, 64 - s), base.resize((s, s), Image.LANCZOS))
    x += s + 10
strip.save(out_png.replace(".png", "-small.png"))
print("ok")
